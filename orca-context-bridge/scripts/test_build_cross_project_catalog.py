#!/usr/bin/env python3
"""Unit tests for build_cross_project_catalog.py (M4 of the cross-project
catalog plan).

Covers: read/write guard contracts (write_only_within / read_only_from),
the asymmetric symlink policy (write side bans every symlink component,
read side accepts a symlinked project root but still bans anything below
it), the three fixture project trees exercising ok/degrade/no-wiki-dir
outcomes, an OS-level read-only-permission isolation control, a fully
hermetic end-to-end run through a stubbed `orca` binary (enumeration
failures, fingerprint/rebuilt semantics, dangling-reference
classification), and a subprocess-isolated check that importing
validate_reusable_capabilities.py has no side effects.

Run with: /usr/bin/python3 -m unittest orca-context-bridge/scripts/test_build_cross_project_catalog.py -v
(from the knowledge root), or plain `/usr/bin/python3 test_build_cross_project_catalog.py`
from this directory.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_cross_project_catalog as bcpc  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _snapshot(root: Path) -> dict[str, tuple]:
    """Recursive lstat snapshot: mode, size, mtime_ns, for every path under
    root, using followlinks=False so it never descends into a symlinked
    subdir (fine for these fixtures -- none has one)."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        for name in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            out[full] = (st.st_mode, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    return out


REUSABLE_CAPS_ALPHA = {
    "schema_version": 1,
    "project": "fixture/alpha",
    "capabilities": [
        {
            "id": "alpha-script",
            "kind": "script",
            "name": "run.py",
            "path": "scripts/run.py",
            "summary": "does a thing",
            "last_verified_at": "2026-08-22T00:00:00Z",
            "depends_on": [],
        }
    ],
}

CONTEXT_WIKI_ALPHA_TEMPLATE = {
    "version": 1,
    "meta": {"content_version": 1, "updated_at": "2026-08-22T00:00:00+00:00"},
    "project": {"path": "PLACEHOLDER"},
    "pages": [
        {"id": "p1", "title": "P1", "path": "a.md", "summary": "s", "status": "live"},
        {"id": "p2", "title": "P2", "path": "b.md", "summary": "s", "status": "live"},
    ],
    "links": [{"from": "p1", "to": "p2", "relation": "documents"}],
}

CLI_INVENTORY_ALPHA = {
    "version": 1,
    "schemaVersion": 1,
    "commandCount": 2,
    "verificationCounts": {"live_read_only_verified": 2},
    "commands": [
        {"command": "a", "summary": "s", "verification": "live_read_only_verified", "boundary": "b"},
        {"command": "b", "summary": "s", "verification": "live_read_only_verified", "boundary": "b"},
    ],
}

CONTEXT_WIKI_BETA = {
    "version": 1,
    "project": {"path": "PLACEHOLDER"},
    "pages": [{"id": "only", "title": "Only", "path": "x.md", "summary": "s", "status": "live"}],
    "links": [],
}


def build_fixture_tree(root: Path) -> None:
    """alpha/ = all 3 allow-listed files + a non-allow-listed wiki/ file +
    .env + .git (the "完善orca shape"); beta/ = orca-context-wiki.json
    only (the 6-project shape); gamma/ = src/, no wiki/ (the 137-path
    majority shape)."""
    alpha = root / "alpha"
    (alpha / "wiki").mkdir(parents=True)
    (alpha / ".git").mkdir()
    (alpha / ".env").write_text("SECRET=nope\n", encoding="utf-8")
    _write_json(alpha / "wiki" / "reusable-capabilities.json", REUSABLE_CAPS_ALPHA)
    wiki_doc = dict(CONTEXT_WIKI_ALPHA_TEMPLATE)
    wiki_doc["project"] = {"path": str(alpha)}
    _write_json(alpha / "wiki" / "orca-context-wiki.json", wiki_doc)
    _write_json(alpha / "wiki" / "orca-cli-capability-inventory.json", CLI_INVENTORY_ALPHA)
    (alpha / "wiki" / "SECRET-do-not-read.json").write_text('{"leak": true}', encoding="utf-8")

    beta = root / "beta"
    (beta / "wiki").mkdir(parents=True)
    beta_doc = dict(CONTEXT_WIKI_BETA)
    beta_doc["project"] = {"path": str(beta)}
    _write_json(beta / "wiki" / "orca-context-wiki.json", beta_doc)

    gamma = root / "gamma"
    (gamma / "src").mkdir(parents=True)
    (gamma / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# T-00 negative control -- mandatory. If this ever fails, that means the
# filesystem is ignoring permission bits (root, or a permission-ignoring
# filesystem); the whole isolation suite would pass vacuously without it.
# Do not "fix" a failure here by deleting the control.
# ---------------------------------------------------------------------------


class NegativeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bcpc-negctrl-"))
        build_fixture_tree(self.tmp)
        _make_readonly(self.tmp)

    def tearDown(self) -> None:
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cannot_create_new_file_in_readonly_wiki_dir(self) -> None:
        with self.assertRaises(PermissionError):
            (self.tmp / "alpha" / "wiki" / "new-file.json").write_text("{}", encoding="utf-8")

    def test_cannot_truncate_existing_file_in_readonly_wiki_dir(self) -> None:
        target = self.tmp / "alpha" / "wiki" / "reusable-capabilities.json"
        with self.assertRaises(PermissionError):
            target.write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# T-01 / T-02 / T-03 / T-04: the three fixture trees, read-only for the
# whole class (proves the aggregator never needs write access to a
# project), plus the allow-list holding under a direct attack.
# ---------------------------------------------------------------------------


class FixtureTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="bcpc-fixtures-"))
        build_fixture_tree(cls.tmp)
        _make_readonly(cls.tmp)
        # Snapshot AFTER the chmod sweep -- T-99 must isolate changes made
        # by process_target() itself, not the permission change that sets
        # up the isolation regime.
        cls.snapshot_before = _snapshot(cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        _make_writable(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_t01_alpha_collects_all_three_sources_ok(self) -> None:
        outcome = bcpc.process_target({"real_path": str(self.tmp / "alpha"), "project_id": "fixture/alpha"})
        self.assertEqual(outcome["status"], "ok")
        sources = outcome["sources"]
        self.assertEqual(sources["reusable-capabilities.json"]["status"], "ok")
        self.assertEqual(sources["orca-context-wiki.json"]["status"], "ok")
        self.assertEqual(sources["orca-cli-capability-inventory.json"]["status"], "ok")
        self.assertEqual(len(outcome["capabilities"]), 1)
        self.assertEqual(outcome["capabilities"][0]["id"], "alpha-script")
        self.assertEqual(len(outcome["wiki_pages"]), 2)
        self.assertIsNotNone(outcome["cli_inventory"])
        self.assertEqual(outcome["cli_inventory"]["command_count"], 2)

    def test_t02_beta_degrades_gracefully(self) -> None:
        outcome = bcpc.process_target({"real_path": str(self.tmp / "beta"), "project_id": "fixture/beta"})
        self.assertEqual(outcome["status"], "ok")  # absent sources are not errors
        sources = outcome["sources"]
        self.assertEqual(sources["orca-context-wiki.json"]["status"], "ok")
        self.assertEqual(sources["reusable-capabilities.json"]["status"], "absent")
        self.assertEqual(sources["orca-cli-capability-inventory.json"]["status"], "absent")

    def test_t03_gamma_no_wiki_dir(self) -> None:
        outcome = bcpc.process_target({"real_path": str(self.tmp / "gamma"), "project_id": "fixture/gamma"})
        self.assertEqual(outcome["status"], "no-wiki-dir")
        self.assertIsNone(outcome["sources"])
        # Compaction: build_project_row() must omit "sources" entirely here.
        target = dict(
            real_path=str(self.tmp / "gamma"), path=str(self.tmp / "gamma"), aliases=[],
            project_id="fixture/gamma", project_id_source="derived", project_id_ambiguous=False,
            display_name="gamma", orca_kinds={"repo"}, repo_ids=set(), worktree_ids=set(),
            branch=None, is_main_worktree=False, is_archived=False, workspace_status=None,
            status="no-wiki-dir",
        )
        row = bcpc.build_project_row(target, None)
        self.assertNotIn("sources", row)

    def test_t04_allowlist_never_opens_secret_or_dotenv(self) -> None:
        outcome = bcpc.process_target({"real_path": str(self.tmp / "alpha"), "project_id": "fixture/alpha"})
        self.assertEqual(set(outcome["sources"].keys()), set(bcpc.ALLOWED_FILENAMES))
        # A direct attempt to read the non-allow-listed file is rejected
        # before any filesystem touch.
        secret = self.tmp / "alpha" / "wiki" / "SECRET-do-not-read.json"
        checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.tmp / "alpha", secret)
        self.assertIsNone(checked)
        self.assertEqual(reason, "filename_not_allowlisted")
        dotenv = self.tmp / "alpha" / ".env"
        checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.tmp / "alpha", dotenv)
        self.assertIsNone(checked)
        self.assertIn(reason, ("filename_not_allowlisted", "not_in_wiki_dir"))

    def test_t99_byte_identical_project_trees_after_full_processing(self) -> None:
        """Strictly stronger than "no PermissionError was raised": proves
        no stray file, cache, or lock was created anywhere under any
        project tree."""
        for name in ("alpha", "beta", "gamma"):
            bcpc.process_target({"real_path": str(self.tmp / name), "project_id": f"fixture/{name}"})
        snapshot_after = _snapshot(self.tmp)
        self.assertEqual(self.snapshot_before, snapshot_after)


# ---------------------------------------------------------------------------
# T-05 / T-06: write_only_within / read_only_from guard truth tables.
# ---------------------------------------------------------------------------


class WriteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bcpc-writeguard-"))
        self.out_dir = self.tmp / "out"
        self.out_dir.mkdir()
        (self.tmp / "projects" / "alpha").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t05_accepts_path_inside_output_dir(self) -> None:
        checked, reason = bcpc.write_only_within(self.out_dir, str(self.out_dir / "catalog.json"))
        self.assertIsNone(reason)
        self.assertEqual(checked, (self.out_dir / "catalog.json").resolve())

    def test_t05_rejects_path_outside_output_dir(self) -> None:
        target = self.tmp / "projects" / "alpha" / "catalog.json"
        checked, reason = bcpc.write_only_within(self.out_dir, str(target))
        self.assertIsNone(checked)
        self.assertEqual(reason, "outside_catalog_dir")

    def test_t05_rejects_dotdot_escape_not_caught_lexically(self) -> None:
        # Path.absolute() does not normalize '..' -- this must be caught by
        # the post-resolve containment check (step 5), not step 3.
        target_str = str(self.out_dir / ".." / "escape.json")
        checked, reason = bcpc.write_only_within(self.out_dir, target_str)
        self.assertIsNone(checked)
        self.assertEqual(reason, "outside_catalog_dir")

    def test_t05_real_write_via_atomic_write_within(self) -> None:
        final_path = self.out_dir / "catalog.json"
        bcpc.atomic_write_within(self.out_dir, final_path, b'{"ok": true}')
        self.assertEqual(final_path.read_bytes(), b'{"ok": true}')
        self.assertEqual(stat.S_IMODE(os.stat(str(final_path)).st_mode), 0o600)

    def test_t06_invalid_and_non_absolute_inputs(self) -> None:
        for bad in (None, "", 123, "relative/catalog.json"):
            checked, reason = bcpc.write_only_within(self.out_dir, bad)
            self.assertIsNone(checked)
            self.assertIn(reason, ("invalid_path", "non_absolute_path"))

    def test_t06_symlink_component_rejected(self) -> None:
        real_sub = self.out_dir / "real_sub"
        real_sub.mkdir()
        link = self.out_dir / "linked_sub"
        os.symlink(str(real_sub), str(link))
        checked, reason = bcpc.write_only_within(self.out_dir, str(link / "catalog.json"))
        self.assertIsNone(checked)
        self.assertEqual(reason, "symlink_path")

    def test_t06_catalog_root_itself_rejected(self) -> None:
        checked, reason = bcpc.write_only_within(self.out_dir, str(self.out_dir))
        self.assertIsNone(checked)
        self.assertEqual(reason, "is_catalog_root")

    def test_t12_toctou_planted_symlink_makes_atomic_write_within_itself_raise(self) -> None:
        """Drives the REAL atomic_write_within() under the TOCTOU condition.

        The previous version of this test called os.open() directly with
        hand-copied flags and a hand-built tmp name the function would never
        generate -- it tested the OS, not the module, and would have kept
        passing if O_EXCL|O_NOFOLLOW were dropped from the production call.
        The tmp name is `.{final}.tmp-{pid}-{ms}`, so pinning time.time()
        makes it predictable and the symlink can be planted at exactly the
        path the function is about to claim."""
        final_path = self.out_dir / "catalog.json"
        final_path.write_bytes(b"original")
        victim = self.out_dir / "victim.txt"
        victim.write_bytes(b"do-not-touch")
        fixed_time = 1234567.0
        planted = self.out_dir / f".{final_path.name}.tmp-{os.getpid()}-{int(fixed_time * 1000)}"
        os.symlink(str(victim), str(planted))

        with mock.patch.object(bcpc.time, "time", return_value=fixed_time):
            # Sanity: the pinned clock really does aim the function at the
            # planted path (otherwise this test could pass vacuously).
            self.assertEqual(int(bcpc.time.time() * 1000), int(fixed_time * 1000))
            with self.assertRaises(FileExistsError):
                bcpc.atomic_write_within(self.out_dir, final_path, b'{"attacker": "wins"}')

        self.assertEqual(victim.read_bytes(), b"do-not-touch")
        self.assertEqual(final_path.read_bytes(), b"original")
        # The planted link must survive: the open failed before the cleanup
        # bracket was entered, and this process must never unlink a path it
        # did not create.
        self.assertTrue(planted.is_symlink())
        planted.unlink()

    def test_t12_replace_onto_symlink_replaces_link_not_target(self) -> None:
        final_path = self.out_dir / "catalog.json"
        victim = self.out_dir / "victim.txt"
        victim.write_bytes(b"do-not-touch")
        os.symlink(str(victim), str(final_path))
        bcpc.atomic_write_within(self.out_dir, final_path, b'{"ok": true}')
        self.assertFalse(final_path.is_symlink())
        self.assertEqual(final_path.read_bytes(), b'{"ok": true}')
        self.assertEqual(victim.read_bytes(), b"do-not-touch")


class ReadGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bcpc-readguard-"))
        self.project = self.tmp / "proj"
        (self.project / "wiki").mkdir(parents=True)
        _write_json(self.project / "wiki" / "reusable-capabilities.json", REUSABLE_CAPS_ALPHA)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _candidate(self) -> Path:
        return self.project / "wiki" / "reusable-capabilities.json"

    def test_t01_valid_candidate_accepted(self) -> None:
        checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.project, self._candidate())
        self.assertIsNone(reason)
        self.assertEqual(checked, self._candidate().resolve())

    def test_t06_invalid_and_non_absolute(self) -> None:
        for bad in (None, "", 123, "relative/reusable-capabilities.json"):
            checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.project, bad)
            self.assertIsNone(checked)
            self.assertIn(reason, ("invalid_path", "non_absolute_path"))

    def test_t06_filename_and_dir_checks(self) -> None:
        wrong_name = self.project / "wiki" / "not-allowed.json"
        wrong_name.write_text("{}", encoding="utf-8")
        checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.project, wrong_name)
        self.assertIsNone(checked)
        self.assertEqual(reason, "filename_not_allowlisted")

        outside_wiki = self.project / "reusable-capabilities.json"
        outside_wiki.write_text("{}", encoding="utf-8")
        checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.project, outside_wiki)
        self.assertIsNone(checked)
        self.assertEqual(reason, "not_in_wiki_dir")

    def test_t11_dangling_symlink_reported_as_missing_or_dangling(self) -> None:
        dangling = self.project / "wiki" / "orca-context-wiki.json"
        os.symlink(str(self.project / "wiki" / "does-not-exist-target.json"), str(dangling))
        checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.project, dangling)
        self.assertIsNone(checked)
        self.assertEqual(reason, "missing_or_dangling")

    def test_t01_nonexistent_candidate_is_missing_or_dangling_not_an_error(self) -> None:
        never_written = self.project / "wiki" / "orca-cli-capability-inventory.json"
        checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.project, never_written)
        self.assertEqual(reason, "missing_or_dangling")

    def test_t10_fifo_rejected_not_a_regular_file(self) -> None:
        fifo_path = self.project / "wiki" / "orca-cli-capability-inventory.json"
        os.mkfifo(str(fifo_path))
        try:
            checked, reason = bcpc.read_only_from(bcpc.ALLOWED_FILENAMES, self.project, fifo_path)
            self.assertIsNone(checked)
            self.assertEqual(reason, "not_a_regular_file")
            # ...and the full read path agrees, without blocking on the FIFO
            # (read_only_from's isfile() pre-check stops it here; O_NONBLOCK
            # on read_one_source's own open closes the residual window where
            # a regular file is swapped for a FIFO in between).
            started = time.monotonic()
            entry, doc = bcpc.read_one_source(self.project, "orca-cli-capability-inventory.json")
            self.assertLess(time.monotonic() - started, 20.0)
            self.assertEqual(entry["status"], "read-rejected")
            self.assertEqual(entry["guard_reason"], "not_a_regular_file")
            self.assertIsNone(doc)
        finally:
            fifo_path.unlink()

    def test_r2_read_one_source_open_requests_o_nonblock(self) -> None:
        """The isfile() pre-check leaves a TOCTOU window; O_NONBLOCK on the
        real open is what makes that window harmless. Pinned so it cannot be
        dropped silently."""
        seen: list[int] = []
        real_open = os.open

        def spy(path, flags, *args):
            seen.append(flags)
            return real_open(path, flags, *args)

        with mock.patch.object(bcpc.os, "open", spy):
            entry, doc = bcpc.read_one_source(self.project, "reusable-capabilities.json")
        self.assertEqual(entry["status"], "ok")
        self.assertIsNotNone(doc)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0] & os.O_NONBLOCK)
        self.assertTrue(seen[0] & os.O_NOFOLLOW)

    def test_t07_symlinked_project_root_accepted(self) -> None:
        link_root = self.tmp / "proj_link"
        os.symlink(str(self.project), str(link_root))
        checked, reason = bcpc.read_only_from(
            bcpc.ALLOWED_FILENAMES, link_root, link_root / "wiki" / "reusable-capabilities.json"
        )
        self.assertIsNone(reason)
        self.assertEqual(checked, self._candidate().resolve())

    def test_t08_wiki_symlink_escaping_root_rejected(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        _write_json(outside / "reusable-capabilities.json", REUSABLE_CAPS_ALPHA)
        escaping_project = self.tmp / "escaping"
        escaping_project.mkdir()
        os.symlink(str(outside), str(escaping_project / "wiki"))
        checked, reason = bcpc.read_only_from(
            bcpc.ALLOWED_FILENAMES, escaping_project, escaping_project / "wiki" / "reusable-capabilities.json"
        )
        self.assertIsNone(checked)
        self.assertEqual(reason, "wiki_escapes_project_root")

    def test_t09_wiki_symlink_inside_root_accepted(self) -> None:
        inside_project = self.tmp / "inside"
        docs = inside_project / "docs"
        docs.mkdir(parents=True)
        _write_json(docs / "reusable-capabilities.json", REUSABLE_CAPS_ALPHA)
        os.symlink(str(docs), str(inside_project / "wiki"))
        checked, reason = bcpc.read_only_from(
            bcpc.ALLOWED_FILENAMES, inside_project, inside_project / "wiki" / "reusable-capabilities.json"
        )
        self.assertIsNone(reason)
        self.assertEqual(checked, (docs / "reusable-capabilities.json").resolve())

    def test_t08_run_continues_when_one_project_wiki_escapes(self) -> None:
        """Never abort the whole run -- one bad project among many degrades
        only that project and every other target is still collected."""
        outside = self.tmp / "outside2"
        outside.mkdir()
        escaping_project = self.tmp / "escaping2"
        escaping_project.mkdir()
        os.symlink(str(outside), str(escaping_project / "wiki"))

        good_outcome = bcpc.process_target({"real_path": str(self.project), "project_id": "fixture/proj"})
        self.assertEqual(good_outcome["status"], "ok")
        bad_outcome = bcpc.process_target({"real_path": str(escaping_project), "project_id": "fixture/escaping"})
        # escaping_project's own wiki/ dir is itself the symlink target, so
        # os.path.isdir(wiki) is True and every candidate file underneath is
        # rejected by the guard -- this project degrades to "partial", it
        # does not raise and does not take the run down with it.
        self.assertEqual(bad_outcome["status"], "partial")
        for name in bcpc.ALLOWED_FILENAMES:
            self.assertEqual(bad_outcome["sources"][name]["guard_reason"], "wiki_escapes_project_root")


# ---------------------------------------------------------------------------
# T-13 path-missing
# ---------------------------------------------------------------------------


class PathMissingTests(unittest.TestCase):
    def test_t13_registered_but_absent_path(self) -> None:
        outcome = bcpc.process_target(
            {"real_path": "/no/such/path/on/this/machine-bcpc-test", "project_id": "fixture/missing"}
        )
        self.assertEqual(outcome["status"], "path-missing")
        self.assertIsNone(outcome["sources"])


# ---------------------------------------------------------------------------
# T-14: importing validate_reusable_capabilities.py must be side-effect-free
# -- pins the coupling introduced by importing derive_expected_project_id.
# ---------------------------------------------------------------------------


class ValidatorImportSideEffectTests(unittest.TestCase):
    def test_t14_import_produces_no_output_no_files_exit_0(self) -> None:
        scripts_dir = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as run_dir:
            before = set(os.listdir(run_dir))
            proc = subprocess.run(
                [sys.executable, "-c", "import validate_reusable_capabilities"],
                cwd=run_dir,
                env={**os.environ, "PYTHONPATH": str(scripts_dir)},
                capture_output=True,
                timeout=15,
            )
            after = set(os.listdir(run_dir))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# T-15: a duplicate top-level key is parse-error, not silently accepted.
# ---------------------------------------------------------------------------


class DuplicateKeyTests(unittest.TestCase):
    def test_t15_duplicate_key_is_parse_error_project_partial(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="bcpc-dupkey-"))
        try:
            project = tmp / "dupproj"
            (project / "wiki").mkdir(parents=True)
            raw = '{"schema_version": 1, "project": "x", "project": "y", "capabilities": []}'
            (project / "wiki" / "reusable-capabilities.json").write_text(raw, encoding="utf-8")
            outcome = bcpc.process_target({"real_path": str(project), "project_id": "fixture/dupproj"})
            self.assertEqual(outcome["status"], "partial")
            self.assertEqual(outcome["sources"]["reusable-capabilities.json"]["status"], "parse-error")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Real-pilot-file tests: process_target() run against the two actual,
# dual-review-verified wiki/ trees on this machine -- not fixtures. Confirms
# real content is opened, read, and summarized correctly rather than only
# ever exercising synthetic fixture content.
#
# F1 (round 3): these tests used to hard-pin values SCRAPED from those live
# files -- capability_count == 11, content_version == 1, command_count ==
# 235, an exact rn邮箱 id-set, page_count == 3. Those files are untracked,
# shared, and edited by other concurrent sessions in this very worktree, so
# the suite went red the moment a 12th capability was added and
# meta.content_version was bumped 1 -> 2. Pinning content_version with
# assertEqual was the worst of them: that counter EXISTS to increment (it is
# this repo's own M2 wiki-freshness design), so it was guaranteed to break
# again on every legitimate wiki edit, forever.
#
# The rule now, applied to all four pin groups:
#
#   * every exact assertion compares the builder's OUTPUT against the same
#     file re-read INDEPENDENTLY in setUp -- never against a literal typed
#     into this file. That tests what the code actually promises ("summarize
#     what is on disk, faithfully") and can never go stale;
#   * every magnitude that can legitimately grow additionally gets an
#     assertGreaterEqual FLOOR against the original 2026-08-22 baseline, so
#     the test still proves it is reading a real, non-trivial fleet rather
#     than passing vacuously on an empty file. (assertGreaterEqual was
#     already the idiom used for page_count at the time; it is now used
#     everywhere it belongs.)
#
# The skip guards check that the pilot PROJECT DIRECTORY exists and that
# any of the three allow-listed files it does have are INDEPENDENTLY
# PARSEABLE, not merely that the directory is present -- the old docstring
# claimed the class "stays hermetic (and still fully green) on a machine
# that does not have this exact fleet checked out", which a
# directory-presence-only guard could not deliver.
#
# This does NOT cover every absence: if the project directory exists but
# `wiki/` (or one of the three files inside it) does not, setUp() records
# that as a genuine "absent" source rather than skipping, and the affected
# assertions fail rather than skip -- correctly, since a project that
# briefly loses one of these pilot files is a real regression, not a
# hermeticity gap. Unreachable on this machine today: both pilot roots and
# all three files are present, and `wiki/` is now git-tracked.
# ---------------------------------------------------------------------------

_REAL_WANSHAN_ORCA = Path("/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca")
_REAL_RN_MAIL = Path("/Volumes/Extreme SSD/Orca/projects/rn邮箱")

# Original (2026-08-22) baseline magnitudes, used ONLY as
# assertGreaterEqual floors. Growth past these is legitimate content
# growth and must never turn the suite red.
_BASELINE_WANSHAN_CAPABILITY_COUNT = 11
_BASELINE_WANSHAN_CONTENT_VERSION = 1
_BASELINE_WANSHAN_COMMAND_COUNT = 235
_BASELINE_WANSHAN_PAGE_COUNT = 1
_BASELINE_RN_MAIL_CAPABILITY_COUNT = 5
_BASELINE_RN_MAIL_PAGE_COUNT = 3


def _dedup_depends_on(raw: object) -> list[str]:
    """Reproduce summarize_reusable_capabilities()'s documented depends_on
    normalization (drop non-strings and empties, order-preserving dedup) so
    the expected value can be derived from the file rather than typed in."""
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str) or not item:
                continue
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out


class _RealPilotBase(unittest.TestCase):
    """Reads only the 3 allow-listed files under each real project's wiki/
    -- same read-only discipline as every other test in this suite, just
    pointed at real content instead of a fixture tree. Never writes
    anywhere; process_target() has no write path at all.

    setUp reads those files a SECOND time, directly and independently of the
    module under test, and every exact assertion below compares the module's
    output against that independent read."""

    project_root: Path = Path("/nonexistent")
    project_id = ""

    def setUp(self) -> None:
        if not self.project_root.is_dir():
            self.skipTest(f"real {self.project_id} project not present on this machine")
        self.real_path = str(self.project_root.resolve())
        self.raw = {}
        for name in bcpc.ALLOWED_FILENAMES:
            path = self.project_root / "wiki" / name
            if not path.is_file():
                self.raw[name] = None
                continue
            try:
                self.raw[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                # A shared, concurrently-edited worktree: a file caught
                # mid-write is a reason to skip with an explicit reason, not
                # to report a red suite for a defect that is not there.
                self.skipTest(f"pilot file {path} is not independently readable right now: {exc}")
        self.outcome = bcpc.process_target({"real_path": self.real_path, "project_id": self.project_id})
        self.sources = self.outcome["sources"]
        self.assertIsNotNone(self.sources)

    def assert_capabilities_agree_with_file(self, floor: int) -> dict:
        """Every capability assertion that is not project-specific."""
        entry = self.sources["reusable-capabilities.json"]
        doc = self.raw["reusable-capabilities.json"]
        self.assertEqual(entry["status"], "ok", entry)
        self.assertEqual(entry["dropped_count"], 0, entry)
        raw_caps = doc["capabilities"]

        # Derived, not scraped: the builder's count is the file's count.
        self.assertEqual(entry["capability_count"], len(self.outcome["capabilities"]))
        self.assertEqual(entry["capability_count"] + entry["dropped_count"], len(raw_caps))
        # ...and a floor, so this cannot pass vacuously on an emptied file.
        self.assertGreaterEqual(entry["capability_count"], floor)

        # The file declares the project id this target was enumerated under.
        self.assertEqual(entry["declared_project"], doc["project"])
        self.assertEqual(entry["declared_project"], self.project_id)
        self.assertTrue(entry["declared_project_matches_derived"])

        # Identity and depends_on normalization, both derived from the file.
        self.assertEqual(
            {c["id"] for c in self.outcome["capabilities"]},
            {c["id"] for c in raw_caps},
        )
        by_id = {c["id"]: c for c in raw_caps}
        for cap in self.outcome["capabilities"]:
            self.assertEqual(
                cap["depends_on_raw"],
                _dedup_depends_on(by_id[cap["id"]].get("depends_on")),
                cap["id"],
            )
        return entry

    def assert_wiki_agrees_with_file(self, floor: int) -> dict:
        entry = self.sources["orca-context-wiki.json"]
        doc = self.raw["orca-context-wiki.json"]
        self.assertEqual(entry["status"], "ok", entry)
        self.assertEqual(entry["dropped_count"], 0, entry)

        self.assertEqual(entry["page_count"], len(self.outcome["wiki_pages"]))
        self.assertEqual(entry["page_count"] + entry["dropped_count"], len(doc["pages"]))
        self.assertGreaterEqual(entry["page_count"], floor)
        self.assertEqual(
            [p["id"] for p in self.outcome["wiki_pages"]],
            [p["id"] for p in doc["pages"]],
        )

        # declared_path is carried through verbatim, and declared_path_matches
        # is the realpath comparison -- asserted as the RELATION, not as a
        # frozen True/False, so a project that later fixes (or acquires) a
        # declared-path drift does not turn this red.
        project_field = doc.get("project")
        declared = project_field.get("path") if isinstance(project_field, dict) else None
        declared = declared if isinstance(declared, str) and declared else None
        self.assertEqual(entry["declared_path"], declared)
        if declared is None:
            self.assertIsNone(entry["declared_path_matches"])
        else:
            self.assertEqual(
                entry["declared_path_matches"],
                os.path.realpath(declared) == self.real_path,
            )
        return entry


class RealWanshanOrcaPilotTests(_RealPilotBase):
    project_root = _REAL_WANSHAN_ORCA
    project_id = "orca/完善orca"

    def test_capabilities_match_the_live_file_and_clear_the_baseline_floor(self) -> None:
        self.assertIn(self.outcome["status"], ("ok", "partial"))
        self.assert_capabilities_agree_with_file(_BASELINE_WANSHAN_CAPABILITY_COUNT)

    def test_wiki_pages_and_content_version_match_the_live_file(self) -> None:
        entry = self.assert_wiki_agrees_with_file(_BASELINE_WANSHAN_PAGE_COUNT)
        doc = self.raw["orca-context-wiki.json"]
        # content_version is a MONOTONIC counter (M2 wiki-freshness): its
        # value is derived from the file, and only a floor is pinned.
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        self.assertEqual(entry["content_version"], meta.get("content_version"))
        self.assertGreaterEqual(entry["content_version"], _BASELINE_WANSHAN_CONTENT_VERSION)
        # As of the 2026-08-22 baseline this project carries a real, known
        # declared-path drift (the wiki still names a pre-SSD-cutover
        # /Users/... path). assert_wiki_agrees_with_file() pins the RELATION
        # rather than the drift, so fixing the drift is not a test failure.
        self.assertIsInstance(entry["declared_path"], str)

    def test_cli_inventory_command_count_matches_the_live_file(self) -> None:
        entry = self.sources["orca-cli-capability-inventory.json"]
        doc = self.raw["orca-cli-capability-inventory.json"]
        self.assertEqual(entry["status"], "ok", entry)
        expected = (
            len(doc["commands"])
            if isinstance(doc.get("commands"), list)
            else doc.get("commandCount")
        )
        self.assertEqual(entry["command_count"], expected)
        self.assertGreaterEqual(entry["command_count"], _BASELINE_WANSHAN_COMMAND_COUNT)
        self.assertIsNotNone(self.outcome["cli_inventory"])
        self.assertEqual(self.outcome["cli_inventory"]["command_count"], entry["command_count"])


class RealRnMailPilotTests(_RealPilotBase):
    project_root = _REAL_RN_MAIL
    project_id = "rn邮箱"

    def test_capabilities_match_the_live_file_and_clear_the_baseline_floor(self) -> None:
        self.assertIn(self.outcome["status"], ("ok", "partial"))
        self.assert_capabilities_agree_with_file(_BASELINE_RN_MAIL_CAPABILITY_COUNT)

    def test_the_planted_cross_project_refs_are_carried_through_as_cross_project(self) -> None:
        """This pilot exists to exercise cross-project references end to end.

        Asserted STRUCTURALLY -- "at least one depends_on entry parses as
        cross-project, and the builder carried it through unchanged" --
        rather than by pinning the literal target strings, which are exactly
        the kind of live, editable content F1 is about. The property the
        pilot was built for is preserved; the brittleness is not."""
        cross = []
        for cap in self.outcome["capabilities"]:
            for raw in cap["depends_on_raw"]:
                parsed = bcpc._parse_depends_on_ref(raw)
                if parsed is not None and parsed[2] == "cross-project":
                    cross.append(raw)
        self.assertGreaterEqual(len(cross), 1, "rn邮箱 pilot no longer declares any cross-project ref")
        # Every one of them really is in the file, byte for byte.
        raw_doc_refs = {
            item
            for cap in self.raw["reusable-capabilities.json"]["capabilities"]
            for item in _dedup_depends_on(cap.get("depends_on"))
        }
        for raw in cross:
            self.assertIn(raw, raw_doc_refs)

    def test_wiki_pages_match_the_live_file(self) -> None:
        entry = self.assert_wiki_agrees_with_file(_BASELINE_RN_MAIL_PAGE_COUNT)
        # This project's wiki declares its own real path, unlike 完善orca's.
        self.assertTrue(entry["declared_path_matches"])

    def test_absent_cli_inventory_is_absent_not_an_error(self) -> None:
        self.assertEqual(self.sources["orca-cli-capability-inventory.json"]["status"], "absent")
        self.assertIsNone(self.outcome["cli_inventory"])


# ---------------------------------------------------------------------------
# Hermetic end-to-end tests via a stubbed `orca` binary -- no dependency on
# the real orca CLI being present, and no dependency on this machine's
# actual project fleet.
# ---------------------------------------------------------------------------


def _write_fake_orca_v2(path: Path, repo_payload: dict, wt_payload: dict) -> None:
    """Write a stub `orca` executable that serves canned repo/worktree
    payloads from JSON sidecar files (rather than embedding them as Python
    source text, to avoid any quoting/escaping hazard)."""
    data_dir = path.parent / f"{path.name}.data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "repo.json").write_text(json.dumps(repo_payload, ensure_ascii=False), encoding="utf-8")
    (data_dir / "wt.json").write_text(json.dumps(wt_payload, ensure_ascii=False), encoding="utf-8")
    script = f"""#!/usr/bin/env python3
import sys, pathlib
argv = sys.argv[1:]
here = pathlib.Path(__file__).resolve().parent / "{data_dir.name}"
if argv[:2] == ["repo", "list"]:
    sys.stdout.write((here / "repo.json").read_text(encoding="utf-8"))
elif argv[:2] == ["worktree", "list"]:
    sys.stdout.write((here / "wt.json").read_text(encoding="utf-8"))
else:
    sys.stdout.write("{{}}")
sys.exit(0)
"""
    path.write_text(script, encoding="utf-8")
    os.chmod(str(path), 0o755)


def _write_fake_orca_failing(path: Path, mode: str) -> None:
    """mode in: nonzero, malformed, ok_false, truncated, mismatch"""
    if mode == "nonzero":
        body = "sys.exit(1)"
    elif mode == "malformed":
        body = 'sys.stdout.write("not json"); sys.exit(0)'
    elif mode == "ok_false":
        body = (
            'import json\n'
            'argv = sys.argv[1:]\n'
            'if argv[:2] == ["repo", "list"]:\n'
            '    print(json.dumps({"id": "x", "ok": False, "result": {}, "_meta": {}}))\n'
            'else:\n'
            '    print(json.dumps({"id": "x", "ok": True, "result": {"worktrees": [], "totalCount": 0, "truncated": False}, "_meta": {}}))\n'
            'sys.exit(0)'
        )
    elif mode == "truncated":
        body = (
            'import json\n'
            'argv = sys.argv[1:]\n'
            'if argv[:2] == ["repo", "list"]:\n'
            '    print(json.dumps({"id": "x", "ok": True, "result": {"repos": []}, "_meta": {}}))\n'
            'else:\n'
            '    print(json.dumps({"id": "x", "ok": True, "result": {"worktrees": [], "totalCount": 0, "truncated": True}, "_meta": {}}))\n'
            'sys.exit(0)'
        )
    elif mode == "mismatch":
        body = (
            'import json\n'
            'argv = sys.argv[1:]\n'
            'if argv[:2] == ["repo", "list"]:\n'
            '    print(json.dumps({"id": "x", "ok": True, "result": {"repos": []}, "_meta": {}}))\n'
            'else:\n'
            '    print(json.dumps({"id": "x", "ok": True, "result": {"worktrees": [], "totalCount": 99, "truncated": False}, "_meta": {}}))\n'
            'sys.exit(0)'
        )
    else:
        raise ValueError(mode)
    script = f"#!/usr/bin/env python3\nimport sys\n{body}\n"
    path.write_text(script, encoding="utf-8")
    os.chmod(str(path), 0o755)


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bcpc-e2e-"))
        self.projects_dir = self.tmp / "projects"
        build_fixture_tree(self.projects_dir)
        self.output_dir = self.tmp / "out"
        self.orca_bin = self.tmp / "fake_orca.py"
        # --output is pinned to DEFAULT_OUTPUT_DIR-or-descendant with NO
        # override of any kind (the hidden --i-understand-output-override
        # flag this class used to pass was deleted: it reopened the very
        # hole the pin closes, letting a write land inside a live project's
        # git tree at exit 0). The pin is redirected the way
        # OutputPinningTests always did it -- by rebinding the module
        # constant, which is a property of this test process and not of the
        # CLI surface.
        self._saved_default = bcpc.DEFAULT_OUTPUT_DIR
        bcpc.DEFAULT_OUTPUT_DIR = self.output_dir

        repos = [
            {"id": "repo-alpha", "path": str(self.projects_dir / "alpha")},
            {"id": "repo-beta", "path": str(self.projects_dir / "beta")},
        ]
        worktrees = [
            {
                "id": "wt-alpha", "path": str(self.projects_dir / "alpha"), "branch": "refs/heads/main",
                "isMainWorktree": True, "isArchived": False, "workspaceStatus": "in-progress",
            },
            {
                "id": "wt-gamma", "path": str(self.projects_dir / "gamma"), "branch": "refs/heads/main",
                "isMainWorktree": True, "isArchived": False, "workspaceStatus": "done",
            },
            {
                "id": "wt-missing", "path": str(self.tmp / "no-such-registered-project"),
                "branch": "refs/heads/main", "isMainWorktree": False, "isArchived": True,
                "workspaceStatus": "archived",
            },
        ]
        self.repo_payload = {"id": "x", "ok": True, "result": {"repos": repos}, "_meta": {}}
        self.wt_payload = {
            "id": "x", "ok": True,
            "result": {"worktrees": worktrees, "totalCount": len(worktrees), "truncated": False},
            "_meta": {},
        }
        _write_fake_orca_v2(self.orca_bin, self.repo_payload, self.wt_payload)

    def tearDown(self) -> None:
        bcpc.DEFAULT_OUTPUT_DIR = self._saved_default
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, extra_args: list[str] | None = None) -> tuple[int, dict]:
        argv = [
            "build", "--orca-bin", str(self.orca_bin), "--output", str(self.output_dir),
            "--json", "--quiet",
        ]
        if extra_args:
            argv += extra_args
        # main() prints to stdout when --json and not --quiet; we passed
        # --quiet so nothing prints -- call cmd_build via main() and read
        # the written catalog.json directly instead.
        code = bcpc.main(argv)
        catalog_path = self.output_dir / bcpc.CATALOG_NAME
        catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.is_file() else None
        return code, catalog

    def _add_project(self, name: str, caps: object = None, wiki: object = None, inventory: object = None) -> Path:
        """Register one more fixture project with the stub `orca` and write
        whichever of the three allow-listed files were supplied."""
        project = self.projects_dir / name
        (project / "wiki").mkdir(parents=True, exist_ok=True)
        if caps is not None:
            if isinstance(caps, str):  # raw text, for deliberately unparseable files
                (project / "wiki" / "reusable-capabilities.json").write_text(caps, encoding="utf-8")
            else:
                _write_json(project / "wiki" / "reusable-capabilities.json", caps)
        if wiki is not None:
            _write_json(project / "wiki" / "orca-context-wiki.json", wiki)
        if inventory is not None:
            _write_json(project / "wiki" / "orca-cli-capability-inventory.json", inventory)
        self.repo_payload["result"]["repos"].append({"id": f"repo-{name}", "path": str(project)})
        _write_fake_orca_v2(self.orca_bin, self.repo_payload, self.wt_payload)
        return project

    @staticmethod
    def _cap(cid: str, kind: str = "script", name: str = "x.py", depends_on: list | None = None) -> dict:
        return {
            "id": cid, "kind": kind, "name": name, "path": f"scripts/{name}",
            "summary": "s", "last_verified_at": None, "depends_on": depends_on or [],
        }

    def test_t01_t02_t03_t13_full_fleet_via_stub(self) -> None:
        code, catalog = self._run()
        self.assertEqual(code, 0)
        self.assertIsNotNone(catalog)
        self.assertEqual(catalog["enumeration"]["repo_count"], 2)
        self.assertEqual(catalog["enumeration"]["worktree_count"], 3)
        # alpha appears once (repo + worktree rows dedup by realpath), beta
        # once (repo-only), gamma once (worktree-only), missing once.
        self.assertEqual(catalog["enumeration"]["scan_target_count"], 4)
        by_path = {p["real_path"]: p for p in catalog["projects"]}
        alpha_row = by_path[str((self.projects_dir / "alpha").resolve())]
        self.assertEqual(alpha_row["status"], "ok")
        self.assertEqual(sorted(alpha_row["orca_kinds"]), ["repo", "worktree"])
        self.assertTrue(alpha_row["orca"]["is_main_worktree"])
        beta_row = by_path[str((self.projects_dir / "beta").resolve())]
        self.assertEqual(beta_row["status"], "ok")
        self.assertEqual(beta_row["orca_kinds"], ["repo"])
        gamma_row = by_path[str((self.projects_dir / "gamma").resolve())]
        self.assertEqual(gamma_row["status"], "no-wiki-dir")
        self.assertNotIn("sources", gamma_row)
        missing_row = by_path[str(Path(os.path.realpath(str(self.tmp / "no-such-registered-project"))))]
        self.assertEqual(missing_row["status"], "path-missing")
        self.assertTrue(missing_row["orca"]["is_archived"])
        self.assertEqual(missing_row["orca"]["workspace_status"], "archived")
        # Never filtered out despite being archived.
        self.assertEqual(catalog["counts"]["degraded_sources"], 0)

    def test_t16_ref_resolution_same_and_cross_project(self) -> None:
        # Add a second project that depends on alpha's capability, one
        # same-project ref that resolves, and one cross-project ref that
        # dangles because the target project has no capabilities file.
        delta = self.projects_dir / "delta"
        (delta / "wiki").mkdir(parents=True)
        _write_json(
            delta / "wiki" / "reusable-capabilities.json",
            {
                "schema_version": 1,
                "project": "fixture/delta",
                "capabilities": [
                    {
                        "id": "delta-cap",
                        "kind": "script",
                        "name": "d.py",
                        "path": "d.py",
                        "summary": "s",
                        "last_verified_at": None,
                        "depends_on": [
                            "alpha:script:run.py",
                            "gamma:script:nonexistent.py",
                        ],
                    }
                ],
            },
        )
        self.repo_payload["result"]["repos"].append({"id": "repo-delta", "path": str(delta)})
        _write_fake_orca_v2(self.orca_bin, self.repo_payload, self.wt_payload)

        code, catalog = self._run()
        self.assertEqual(code, 0)
        delta_cap = next(c for c in catalog["capabilities"] if c["id"] == "delta-cap")
        resolved = [d for d in delta_cap["depends_on"] if d["state"] == "resolved"]
        unresolved = [d for d in delta_cap["depends_on"] if d["state"] == "unresolved"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["target_global_id"], "alpha#alpha-script")
        self.assertEqual(len(unresolved), 1)
        # gamma is enumerated (a worktree row exists) but has no wiki/ dir
        # at all, let alone a capabilities file.
        unresolved_ref = next(
            r for r in catalog["unresolved_references"] if r["target_project_id"] == "gamma"
        )
        self.assertEqual(unresolved_ref["reason"], "project-has-no-capabilities-file")
        self.assertEqual(unresolved_ref["target_project_state"], "enumerated-not-adopted")
        rev = catalog["capability_reverse_index"]["alpha#alpha-script"]
        self.assertEqual(rev["in_degree"], 1)
        self.assertEqual(rev["referencing_project_ids"], ["delta"])

    def test_t17_enumeration_failures_exit_4_and_previous_catalog_untouched(self) -> None:
        code, catalog = self._run()
        self.assertEqual(code, 0)
        before_bytes = (self.output_dir / bcpc.CATALOG_NAME).read_bytes()

        for mode in ("nonzero", "malformed", "ok_false", "truncated", "mismatch"):
            with self.subTest(mode=mode):
                failing_bin = self.tmp / f"failing_{mode}.py"
                _write_fake_orca_failing(failing_bin, mode)
                code2 = bcpc.main(
                    ["build", "--orca-bin", str(failing_bin), "--output", str(self.output_dir), "--quiet"]
                )
                self.assertEqual(code2, 4)
                after_bytes = (self.output_dir / bcpc.CATALOG_NAME).read_bytes()
                self.assertEqual(before_bytes, after_bytes)

    def test_t18_fingerprint_rebuilt_semantics(self) -> None:
        code1, catalog1 = self._run()
        self.assertEqual(code1, 0)
        fp1 = catalog1["content_fingerprint"]
        gen1 = catalog1["generated_at"]

        code2, catalog2 = self._run()
        self.assertEqual(code2, 0)
        self.assertEqual(catalog2["content_fingerprint"], fp1)
        self.assertEqual(catalog2["generated_at"], gen1)  # carried forward
        self.assertGreaterEqual(catalog2["verified_at"], catalog1["verified_at"])

        # touching a source's mtime without changing its content must not
        # flip rebuilt -- content is what's fingerprinted, not mtime.
        cap_file = self.projects_dir / "alpha" / "wiki" / "reusable-capabilities.json"
        os.utime(str(cap_file), None)
        code3, catalog3 = self._run()
        self.assertEqual(catalog3["content_fingerprint"], fp1)
        self.assertEqual(catalog3["generated_at"], gen1)

        # editing content must flip it. now_iso() has 1-second resolution,
        # so sleep across a wall-clock second boundary to make the
        # generated_at inequality assertion meaningful rather than
        # incidentally true.
        time.sleep(1.1)
        doc = json.loads(cap_file.read_text(encoding="utf-8"))
        doc["capabilities"][0]["summary"] = "a different summary now"
        _write_json(cap_file, doc)
        code4, catalog4 = self._run()
        self.assertNotEqual(catalog4["content_fingerprint"], fp1)
        self.assertNotEqual(catalog4["generated_at"], gen1)

        # --force always reports rebuilt via a fresh generated_at even with
        # unchanged content.
        time.sleep(1.1)
        code5, catalog5 = self._run(["--force"])
        self.assertEqual(code5, 0)
        self.assertNotEqual(catalog5["generated_at"], catalog4["generated_at"])

    # -----------------------------------------------------------------
    # B1: every wiki page carries a namespaced global_id.
    # -----------------------------------------------------------------

    def test_b1_every_wiki_page_has_a_namespaced_global_id(self) -> None:
        code, catalog = self._run()
        self.assertEqual(code, 0)
        self.assertTrue(catalog["wiki_pages"])
        for page in catalog["wiki_pages"]:
            self.assertIn("global_id", page)
            self.assertEqual(page["global_id"], f"{page['project_id']}#page:{page['id']}")
            self.assertFalse(page["duplicate_page_global_id"])
        page_gids = {p["global_id"] for p in catalog["wiki_pages"]}
        cap_gids = {c["global_id"] for c in catalog["capabilities"]}
        self.assertEqual(page_gids & cap_gids, set())
        self.assertIn("alpha#page:p1", page_gids)
        self.assertEqual(catalog["ambiguous_page_global_ids"], [])

    def test_b1_a_capability_id_that_would_collide_with_a_page_gid_is_dropped(self) -> None:
        """The adversarial case the assertion above cannot express.

        On clean fixture data `page_gids & cap_gids == set()` passes
        vacuously -- nothing in the fixtures can collide. A capability with
        id "page:home" alongside a page with id "home" produces the SAME
        global_id in the same project, and the capability-side duplicate
        detection cannot see it (it only ever compares capability global_ids
        against each other). What actually keeps the two namespaces disjoint
        is ID_RE on the capability id, so that is what this test pins."""
        self._add_project(
            "collide",
            caps={
                "schema_version": 1, "project": "collide",
                "capabilities": [
                    self._cap("page:home", "skill", "s1"),   # would mint "collide#page:home"
                    self._cap("UPPER", "skill", "s2"),       # ID_RE is lowercase-only
                    self._cap("trailing-", "skill", "s3"),   # ID_RE forbids a trailing '-'
                    self._cap("legit-cap", "skill", "s4"),
                ],
            },
            wiki={
                "version": 1, "meta": {"content_version": 1},
                "project": {"path": str(self.projects_dir / "collide")},
                "pages": [{"id": "home", "title": "Home", "path": "h.md", "summary": "s", "status": "live"}],
                "links": [],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)  # three dropped entries -> partial -> degraded -> exit 1

        entry = next(p for p in catalog["projects"] if p["project_id"] == "collide")["sources"][
            "reusable-capabilities.json"
        ]
        self.assertEqual(entry["status"], "partial")
        self.assertEqual(entry["dropped_count"], 3)
        self.assertEqual(entry["capability_count"], 1)
        self.assertEqual(
            {c["id"] for c in catalog["capabilities"] if c["project_id"] == "collide"}, {"legit-cap"}
        )
        # The colliding global_id was never minted at all.
        cap_gids = {c["global_id"] for c in catalog["capabilities"]}
        page_gids = {p["global_id"] for p in catalog["wiki_pages"]}
        self.assertIn("collide#page:home", page_gids)
        self.assertNotIn("collide#page:home", cap_gids)
        self.assertEqual(page_gids & cap_gids, set())

    def test_b1_b_duplicate_page_ids_are_flagged_and_retained_not_dropped(self) -> None:
        """Two pages sharing an id in one project mint one global_id for two
        rows, so the natural consumer idiom
        `{p["global_id"]: p for p in wiki_pages}` silently keeps one and
        loses the other. Both rows stay in wiki_pages[] -- this tool does
        not own orca-context-wiki.json's schema -- but the collision is now
        flagged on every colliding row and listed top-level."""
        self._add_project(
            "dupp",
            wiki={
                "version": 1, "meta": {"content_version": 1},
                "project": {"path": str(self.projects_dir / "dupp")},
                "pages": [
                    {"id": "dup", "title": "First", "path": "a.md", "summary": "s", "status": "live"},
                    {"id": "dup", "title": "Second", "path": "b.md", "summary": "s", "status": "live"},
                    {"id": "solo", "title": "Solo", "path": "c.md", "summary": "s", "status": "live"},
                ],
                "links": [],
            },
        )
        code, catalog = self._run()
        # F7 (round 3): this and the nine other sites in this class used to
        # be `assertIn(code, (0, 1))`. The exit code is a pure deterministic
        # function of one field -- `return 1 if catalog["degraded"] else 0`
        # -- so accepting both values made every one of them structurally
        # blind to a regression that flips a fixture clean <-> degraded.
        # None of these fixtures drops an entry, so 0 is the only correct
        # answer and is now what is asserted.
        self.assertEqual(code, 0)

        rows = [p for p in catalog["wiki_pages"] if p["project_id"] == "dupp"]
        self.assertEqual(len(rows), 3)  # retained, never dropped
        self.assertEqual({r["title"] for r in rows}, {"First", "Second", "Solo"})
        for row in rows:
            self.assertEqual(row["duplicate_page_global_id"], row["id"] == "dup")
        self.assertIn("dupp#page:dup", catalog["ambiguous_page_global_ids"])
        self.assertNotIn("dupp#page:solo", catalog["ambiguous_page_global_ids"])
        self.assertEqual(catalog["counts"]["ambiguous_page_global_ids"], 1)
        # page_count / counts.wiki_pages count ROWS, as documented.
        entry = next(p for p in catalog["projects"] if p["project_id"] == "dupp")["sources"][
            "orca-context-wiki.json"
        ]
        self.assertEqual(entry["page_count"], 3)
        self.assertEqual(entry["dropped_count"], 0)
        self.assertEqual(entry["status"], "ok")
        # The measured consumer-side loss the flag exists to warn about.
        joined = {p["global_id"]: p for p in rows}
        self.assertEqual(len(joined), 2)

    # -----------------------------------------------------------------
    # B2: source_status_histogram's denominator is every scan target, not
    # only the projects that happen to have a `sources` dict.
    # -----------------------------------------------------------------

    def test_b2_source_status_histogram_denominator_is_all_scan_targets(self) -> None:
        code, catalog = self._run()
        self.assertEqual(code, 0)
        scan_targets = catalog["counts"]["scan_targets"]
        hist = catalog["counts"]["source_status_histogram"]
        self.assertEqual(set(hist.keys()), set(bcpc.ALLOWED_FILENAMES))
        for name in bcpc.ALLOWED_FILENAMES:
            self.assertEqual(
                sum(hist[name].values()), scan_targets,
                f"{name} histogram must account for every scan target, got {hist[name]}",
            )
        # gamma (no wiki/) and the path-missing row contribute "absent" for
        # all three files even though neither has a `sources` key at all.
        by_path = {p["real_path"]: p for p in catalog["projects"]}
        gamma_row = by_path[str((self.projects_dir / "gamma").resolve())]
        self.assertNotIn("sources", gamma_row)
        self.assertEqual(hist["reusable-capabilities.json"]["ok"], 1)  # alpha only
        self.assertEqual(hist["reusable-capabilities.json"]["absent"], scan_targets - 1)

    # -----------------------------------------------------------------
    # P2-1: duplicate (kind, name) inside ONE project.
    # -----------------------------------------------------------------

    def test_p2_1_duplicate_kind_name_resolves_to_neither_and_is_recorded(self) -> None:
        self._add_project(
            "dupkey",
            caps={
                "schema_version": 1, "project": "dupkey",
                "capabilities": [
                    self._cap("first-cap", "script", "same.py"),
                    self._cap("second-cap", "script", "same.py"),
                ],
            },
        )
        self._add_project(
            "refdup",
            caps={
                "schema_version": 1, "project": "refdup",
                "capabilities": [self._cap("ref-cap", "script", "r.py", ["dupkey:script:same.py"])],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        ref_key = "dupkey:script:same.py"
        # Neither duplicate wins the index -- the key is simply not in it.
        self.assertNotIn(ref_key, catalog["capability_ref_index"])
        # This is the ONE case that is genuinely "ambiguous": the literal
        # ref_key string really is claimed by two capabilities.
        self.assertIn(ref_key, catalog["ambiguous_ref_keys"])
        self.assertIn(
            {"ref_key": ref_key, "reason": "duplicate-ref-key"}, catalog["excluded_ref_keys"]
        )
        # Both entries still appear in capabilities[] and are flagged.
        dups = [c for c in catalog["capabilities"] if c["ref_key"] == ref_key]
        self.assertEqual(len(dups), 2)
        for cap in dups:
            self.assertTrue(cap["duplicate_ref_key"])
            self.assertFalse(cap["duplicate_global_id"])  # ids differ

        # The inbound reference resolves as unresolved, pointing at neither.
        ref_cap = next(c for c in catalog["capabilities"] if c["id"] == "ref-cap")
        dep = ref_cap["depends_on"][0]
        self.assertEqual(dep["state"], "unresolved")
        self.assertNotIn("target_global_id", dep)
        unresolved = next(r for r in catalog["unresolved_references"] if r["ref_key"] == ref_key)
        # Deliberately still "capability-not-found": unlike the excluded
        # cases below, "which of the two did you mean?" has no answer at
        # all, and this case reports itself through ambiguous_ref_keys[].
        # The precise axis is on the row either way.
        self.assertEqual(unresolved["reason"], "capability-not-found")
        self.assertEqual(unresolved["target_excluded_reason"], "duplicate-ref-key")
        # Neither duplicate absorbed the inbound edge.
        for cap in dups:
            self.assertEqual(catalog["capability_reverse_index"][cap["global_id"]]["in_degree"], 0)
            self.assertEqual(catalog["capability_reverse_index"][cap["global_id"]]["referenced_by"], [])

    # -----------------------------------------------------------------
    # P2-2: duplicate id inside ONE project.
    # -----------------------------------------------------------------

    def test_p2_2_duplicate_id_keeps_neither_reverse_index_row(self) -> None:
        self._add_project(
            "dupid",
            caps={
                "schema_version": 1, "project": "dupid",
                "capabilities": [
                    self._cap("same-cap", "script", "a.py"),
                    self._cap("same-cap", "skill", "b-skill"),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        global_id = "dupid#same-cap"
        # No merged/overwritten reverse row survives for either entry.
        self.assertNotIn(global_id, catalog["capability_reverse_index"])
        self.assertIn(global_id, catalog["ambiguous_global_ids"])
        dups = [c for c in catalog["capabilities"] if c["global_id"] == global_id]
        self.assertEqual(len(dups), 2)
        for cap in dups:
            self.assertTrue(cap["duplicate_global_id"])
            # An ambiguous global_id also bars its ref_key from the forward
            # index: resolving to an entry with no trustworthy reverse row
            # is worse than not resolving. (Judgment call A -- keeping this
            # bar is what stops the resolution loop's direct
            # capability_reverse_index[...] lookup from raising KeyError and
            # killing the whole fleet run.)
            self.assertNotIn(cap["ref_key"], catalog["capability_ref_index"])
            self.assertIn(
                {"ref_key": cap["ref_key"], "reason": "duplicate-global-id"},
                catalog["excluded_ref_keys"],
            )
            # ...but these two ref_keys are NOT ambiguous. Each has
            # multiplicity exactly 1: "dupid:script:a.py" and
            # "dupid:skill:b-skill" are distinct strings claimed by one
            # capability apiece. Listing them as "ambiguous" (which the old
            # single-list design did, and an older version of this very test
            # pinned as intended) published a provably false claim and made
            # counts.ambiguous_ref_keys a mislabelled exclusion count.
            self.assertNotIn(cap["ref_key"], catalog["ambiguous_ref_keys"])
        self.assertEqual(catalog["counts"]["ambiguous_global_ids"], 1)
        self.assertEqual(catalog["counts"]["ambiguous_ref_keys"], 0)
        self.assertEqual(catalog["counts"]["excluded_ref_keys"], 2)

    def test_r7_excluded_ref_keys_and_ambiguous_ref_keys_are_different_sets(self) -> None:
        """Both exclusion shapes in one run, so the split shows up as a
        contrast rather than as two separate one-sided assertions."""
        self._add_project(
            "bothx",
            caps={
                "schema_version": 1, "project": "bothx",
                "capabilities": [
                    # duplicated (kind, name) -> genuinely ambiguous ref_key
                    self._cap("cap-a", "script", "shared.py"),
                    self._cap("cap-b", "script", "shared.py"),
                    # duplicated id, distinct (kind, name) -> unique ref_keys
                    self._cap("twin", "skill", "left"),
                    self._cap("twin", "config-pattern", "right"),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        ambiguous = set(catalog["ambiguous_ref_keys"])
        excluded = {row["ref_key"]: row["reason"] for row in catalog["excluded_ref_keys"]}
        self.assertEqual(ambiguous, {"bothx:script:shared.py"})
        self.assertEqual(
            excluded,
            {
                "bothx:script:shared.py": "duplicate-ref-key",
                "bothx:skill:left": "duplicate-global-id",
                "bothx:config-pattern:right": "duplicate-global-id",
            },
        )
        self.assertTrue(ambiguous.issubset(set(excluded)))
        # Judgment call A's crash-prevention property, re-verified end to
        # end: every excluded ref_key is absent from the forward index, and
        # every value still IN the forward index has a reverse row -- which
        # is exactly what makes the resolution loop's direct subscript safe.
        for ref_key in excluded:
            self.assertNotIn(ref_key, catalog["capability_ref_index"])
        for global_id in catalog["capability_ref_index"].values():
            self.assertIn(global_id, catalog["capability_reverse_index"])

    # -----------------------------------------------------------------
    # P3-5: a capability that depends_on itself.
    # -----------------------------------------------------------------

    def test_p3_5_self_reference_does_not_inflate_in_degree(self) -> None:
        self._add_project(
            "selfy",
            caps={
                "schema_version": 1, "project": "selfy",
                "capabilities": [self._cap("self-cap", "script", "s.py", ["script:s.py"])],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        cap = next(c for c in catalog["capabilities"] if c["id"] == "self-cap")
        dep = cap["depends_on"][0]
        self.assertEqual(dep["state"], "self-reference")
        self.assertEqual(dep["target_global_id"], "selfy#self-cap")

        rev = catalog["capability_reverse_index"]["selfy#self-cap"]
        self.assertEqual(rev["in_degree"], 0)
        self.assertEqual(rev["referenced_by"], [])
        self.assertEqual(rev["referencing_project_ids"], [])

        self.assertEqual(len(catalog["self_references"]), 1)
        self.assertEqual(catalog["self_references"][0]["capability_global_id"], "selfy#self-cap")
        self.assertEqual(catalog["counts"]["self_reference_refs"], 1)
        # A self-reference is neither resolved nor unresolved, and the three
        # state counts still add up to the total.
        counts = catalog["counts"]
        self.assertEqual(
            counts["resolved_refs"] + counts["unresolved_refs"] + counts["self_reference_refs"],
            counts["depends_on_refs"],
        )

    def test_p3_5_cross_project_spelled_self_reference_also_caught(self) -> None:
        """The redundant "own project id" spelling reaches the same state."""
        self._add_project(
            "selfy2",
            caps={
                "schema_version": 1, "project": "selfy2",
                "capabilities": [self._cap("s2", "script", "t.py", ["selfy2:script:t.py"])],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)
        cap = next(c for c in catalog["capabilities"] if c["id"] == "s2")
        dep = cap["depends_on"][0]
        self.assertEqual(dep["state"], "self-reference")
        self.assertTrue(dep["redundant_spelling"])
        self.assertEqual(catalog["capability_reverse_index"]["selfy2#s2"]["in_degree"], 0)

    def test_r6_self_reference_survives_its_ref_key_being_barred_from_the_index(self) -> None:
        """P3-5 x P2-1 interaction.

        Self-loop detection used to work by asking the forward index
        "what does this ref_key resolve to?" and comparing the answer to my
        own global_id. A capability with a duplicate (kind, name) sibling has
        its ref_key removed from that index, so the lookup returned None and
        the self-reference was silently reclassified as a DANGLING reference
        to a capability sitting in its own capabilities[] -- disagreeing with
        the validator's self_reference verdict, which is precisely the
        disagreement the self-reference state exists to prevent. Structural
        detection (ref_key vs. the capability's own ref_key) cannot be
        defeated that way."""
        self._add_project(
            "sd",
            caps={
                "schema_version": 1, "project": "sd",
                "capabilities": [
                    self._cap("cap-a", "script", "same.py", ["script:same.py"]),
                    self._cap("cap-b", "script", "same.py"),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        ref_key = "sd:script:same.py"
        self.assertNotIn(ref_key, catalog["capability_ref_index"])  # barred, as P2-1 requires

        cap_a = next(c for c in catalog["capabilities"] if c["id"] == "cap-a")
        dep = cap_a["depends_on"][0]
        self.assertEqual(dep["state"], "self-reference")
        self.assertEqual(dep["target_global_id"], "sd#cap-a")
        self.assertEqual([r["capability_global_id"] for r in catalog["self_references"]], ["sd#cap-a"])
        self.assertEqual(catalog["counts"]["self_reference_refs"], 1)
        # ...and it is NOT filed as an unresolved/dangling reference.
        self.assertEqual([r for r in catalog["unresolved_references"] if r["ref_key"] == ref_key], [])
        for cap in (c for c in catalog["capabilities"] if c["project_id"] == "sd"):
            self.assertEqual(catalog["capability_reverse_index"][cap["global_id"]]["in_degree"], 0)

    # -----------------------------------------------------------------
    # R5: "the target exists but is barred from being a join target" is not
    # "the target project does not have that capability".
    # -----------------------------------------------------------------

    def test_r5_reference_to_an_excluded_target_is_not_reported_as_not_found(self) -> None:
        self._add_project(
            "pa",
            caps={
                "schema_version": 1, "project": "pa",
                "capabilities": [
                    self._cap("twin", "script", "alpha.py"),
                    self._cap("twin", "skill", "beta"),   # same id, distinct (kind, name)
                ],
            },
        )
        self._add_project(
            "pb",
            caps={
                "schema_version": 1, "project": "pb",
                "capabilities": [self._cap("pb-cap", "script", "b.py", ["pa:script:alpha.py"])],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        ref = next(r for r in catalog["unresolved_references"] if r["ref_key"] == "pa:script:alpha.py")
        self.assertEqual(ref["reason"], "capability-ambiguous")
        self.assertEqual(ref["target_project_state"], "in-catalog-target-excluded")
        self.assertEqual(ref["target_excluded_reason"], "duplicate-global-id")
        # The claim the old "capability-not-found" reason made is false: the
        # target really is right there in capabilities[].
        self.assertTrue(
            any(c["ref_key"] == "pa:script:alpha.py" for c in catalog["capabilities"])
        )
        # The contrast -- a reference to a capability that genuinely is not
        # there -- is its own test below, not a conditional in this one.

    def test_r5_genuinely_absent_capability_still_reports_not_found(self) -> None:
        """The contrast case, as its own test rather than a conditional."""
        self._add_project(
            "present",
            caps={
                "schema_version": 1, "project": "present",
                "capabilities": [self._cap("real-cap", "script", "real.py")],
            },
        )
        self._add_project(
            "seeker",
            caps={
                "schema_version": 1, "project": "seeker",
                "capabilities": [self._cap("sk", "script", "s.py", ["present:script:ghost.py"])],
            },
        )
        code, catalog = self._run()
        ref = next(r for r in catalog["unresolved_references"] if r["ref_key"] == "present:script:ghost.py")
        self.assertEqual(ref["reason"], "capability-not-found")
        self.assertEqual(ref["target_project_state"], "in-catalog-with-capabilities")
        self.assertIsNone(ref["target_excluded_reason"])

    # -----------------------------------------------------------------
    # R3: a repeated depends_on ref must not inflate in_degree.
    # -----------------------------------------------------------------

    def test_r3_duplicate_depends_on_string_credits_one_edge_and_is_recorded(self) -> None:
        """One referencing capability listing the identical ref three times
        used to produce in_degree 3 and three referenced_by rows, while
        referencing_project_ids -- built in the same loop, from the same
        data -- was correctly deduped: the catalog contradicted itself and
        published a wrong number in its headline graph metric at exit 0."""
        self._add_project(
            "tgt",
            caps={
                "schema_version": 1, "project": "tgt",
                "capabilities": [self._cap("target-cap", "script", "t.py")],
            },
        )
        self._add_project(
            "src",
            caps={
                "schema_version": 1, "project": "src",
                "capabilities": [
                    self._cap("src-cap", "script", "s.py",
                              ["tgt:script:t.py", "tgt:script:t.py", "tgt:script:t.py"]),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        rev = catalog["capability_reverse_index"]["tgt#target-cap"]
        self.assertEqual(rev["in_degree"], 1)
        self.assertEqual(len(rev["referenced_by"]), 1)
        self.assertEqual(rev["referencing_project_ids"], ["src"])
        # The graph invariant the two dedups jointly buy.
        for row in catalog["capability_reverse_index"].values():
            self.assertEqual(row["in_degree"], len(row["referenced_by"]))

        src_cap = next(c for c in catalog["capabilities"] if c["id"] == "src-cap")
        self.assertEqual(len(src_cap["depends_on"]), 1)
        self.assertEqual(src_cap["depends_on"][0]["state"], "resolved")
        # Recorded, not silently normalized away.
        self.assertEqual(src_cap["duplicate_depends_on_count"], 2)
        self.assertEqual(catalog["counts"]["duplicate_depends_on_entries"], 2)
        self.assertEqual(catalog["counts"]["resolved_refs"], 1)
        counts = catalog["counts"]
        self.assertEqual(
            counts["resolved_refs"] + counts["unresolved_refs"] + counts["self_reference_refs"],
            counts["depends_on_refs"],
        )

    def test_r3_two_spellings_of_the_same_target_credit_one_edge(self) -> None:
        """The other way one capability can credit one target twice: two
        DIFFERENT legal raw strings ("script:t.py" and "own:script:t.py"
        from inside `own`) that resolve to the same global_id. Both entries
        are kept and both are still "resolved" -- only the second edge is
        suppressed, and flagged where it happened."""
        self._add_project(
            "own",
            caps={
                "schema_version": 1, "project": "own",
                "capabilities": [
                    self._cap("own-target", "script", "t.py"),
                    self._cap("own-src", "script", "s.py", ["script:t.py", "own:script:t.py"]),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        rev = catalog["capability_reverse_index"]["own#own-target"]
        self.assertEqual(rev["in_degree"], 1)
        self.assertEqual(len(rev["referenced_by"]), 1)

        src = next(c for c in catalog["capabilities"] if c["id"] == "own-src")
        self.assertEqual([d["state"] for d in src["depends_on"]], ["resolved", "resolved"])
        self.assertNotIn("duplicate_edge", src["depends_on"][0])
        self.assertTrue(src["depends_on"][1]["duplicate_edge"])
        self.assertTrue(src["depends_on"][1]["redundant_spelling"])
        self.assertEqual(src["duplicate_depends_on_count"], 0)  # the raw strings differ
        self.assertEqual(catalog["counts"]["duplicate_edges"], 1)

    # -----------------------------------------------------------------
    # Grok round: id length is enforced alongside ID_RE (shared grammar),
    # non-string depends_on entries are counted instead of vanishing, and
    # a malformed `links` field degrades the wiki source instead of being
    # silently coerced to [].
    # -----------------------------------------------------------------

    def test_grok_p3_capability_id_over_id_max_len_is_dropped(self) -> None:
        """ID_RE alone has no upper bound on length -- a capability id of
        65 characters matches the regex cleanly and used to mint a
        publishable global_id that the M3 validator's own hard ID_MAX_LEN
        check would reject. ID_MAX_LEN is now imported and enforced
        alongside ID_RE, the same shared-grammar precedent as `kind`."""
        overlong = "a" * 65
        self.assertTrue(bcpc.ID_RE.match(overlong), "fixture must actually match ID_RE to prove length is the gate")
        self._add_project(
            "longid",
            caps={
                "schema_version": 1, "project": "longid",
                "capabilities": [
                    self._cap(overlong, "script", "over.py"),
                    self._cap("fine", "script", "ok.py"),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)
        ids = {c["id"] for c in catalog["capabilities"] if c["project_id"] == "longid"}
        self.assertEqual(ids, {"fine"})
        self.assertNotIn(f"longid#{overlong}", catalog["capability_reverse_index"])
        row = next(p for p in catalog["projects"] if p["project_id"] == "longid")
        self.assertEqual(row["sources"]["reusable-capabilities.json"]["dropped_count"], 1)
        self.assertEqual(row["sources"]["reusable-capabilities.json"]["status"], "partial")

    def test_grok_p3_non_string_depends_on_entry_is_counted_not_silent(self) -> None:
        """A non-string (or empty-string) depends_on entry used to vanish
        via a silent `continue` -- real information loss, unlike the
        lossless raw-string dedup R3 covers -- with no trace in
        dropped_count, malformed_depends_on_count, or the source status.
        It is now counted and made visible without touching the
        capability-count invariant dropped_count protects."""
        self._add_project(
            "baddep",
            caps={
                "schema_version": 1, "project": "baddep",
                "capabilities": [
                    self._cap("has-bad-dep", "script", "s.py", [1, None, "", "script:real.py"]),
                    self._cap("real", "script", "real.py"),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)
        cap = next(c for c in catalog["capabilities"] if c["id"] == "has-bad-dep")
        # Only the one genuinely-legal string survives.
        self.assertEqual(len(cap["depends_on"]), 1)
        self.assertEqual(cap["malformed_depends_on_count"], 3)
        # dropped_count keeps its existing meaning (capabilities removed
        # from caps[]) -- both capabilities here are kept, so it stays 0.
        row = next(p for p in catalog["projects"] if p["project_id"] == "baddep")
        caps_source = row["sources"]["reusable-capabilities.json"]
        self.assertEqual(caps_source["dropped_count"], 0)
        self.assertEqual(caps_source["status"], "partial")
        self.assertEqual(catalog["counts"]["malformed_depends_on_entries"], 3)

    def test_grok_p3_links_not_a_list_degrades_instead_of_silent_empty(self) -> None:
        """`links` present but wrong-typed used to be coerced to [] with the
        source still reported "ok" -- the same "wrong type reported as
        healthy" shape `pages` gets treated as schema-invalid for, just
        applied inconsistently. Absence of `links` (the common, legitimate
        case) must stay silent; presence-with-the-wrong-type must not."""
        self._add_project(
            "badlinks",
            wiki={
                "version": 1,
                "pages": [{"id": "p1", "title": "P1", "path": "p1.md", "summary": "s", "status": "live"}],
                "links": {"from": "p1", "to": "p1", "relation": "self"},  # should be a list
            },
        )
        self._add_project(
            "nolinks",
            wiki={
                "version": 1,
                "pages": [{"id": "p1", "title": "P1", "path": "p1.md", "summary": "s", "status": "live"}],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)
        by_project = {p["project_id"]: p for p in catalog["projects"]}
        bad_wiki = by_project["badlinks"]["sources"]["orca-context-wiki.json"]
        self.assertTrue(bad_wiki["links_malformed"])
        self.assertEqual(bad_wiki["status"], "partial")
        self.assertEqual(bad_wiki["link_count"], 0)
        # Absence stays a non-issue: the project is entirely optional here.
        good_wiki = by_project["nolinks"]["sources"]["orca-context-wiki.json"]
        self.assertFalse(good_wiki["links_malformed"])
        self.assertEqual(good_wiki["status"], "ok")
        self.assertEqual(catalog["counts"]["projects_with_malformed_links"], 1)

    # -----------------------------------------------------------------
    # R4: `name` is validated the same way `id` and `kind` are.
    # -----------------------------------------------------------------

    def test_r4_unaddressable_names_are_dropped_not_silently_indexed(self) -> None:
        """A name carrying ':' (or leading/trailing whitespace, or a control
        character) was admitted to capability_ref_index under a ref_key that
        _parse_depends_on_ref() can never produce -- an unreachable entry
        masquerading as a live resolution target, the exact defect the
        `kind` enum check closes on the other half of the same key."""
        self._add_project(
            "badname",
            caps={
                "schema_version": 1, "project": "badname",
                "capabilities": [
                    self._cap("has-colon", "script", "a:b.py"),
                    self._cap("has-lead", "script", " lead.py"),
                    self._cap("has-tab", "script", "tab\there.py"),
                    self._cap("fine", "script", "fine.py"),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)

        entry = next(p for p in catalog["projects"] if p["project_id"] == "badname")["sources"][
            "reusable-capabilities.json"
        ]
        self.assertEqual(entry["capability_count"], 1)
        self.assertEqual(entry["dropped_count"], 3)
        self.assertEqual(entry["status"], "partial")
        self.assertEqual(
            {c["id"] for c in catalog["capabilities"] if c["project_id"] == "badname"}, {"fine"}
        )
        for unreachable in ("badname:script:a:b.py", "badname:script: lead.py"):
            self.assertNotIn(unreachable, catalog["capability_ref_index"])
            # ...and could never have been addressed anyway.
            self.assertIsNone(bcpc._parse_depends_on_ref(unreachable))
        # Every ref_key that DID make it into the index is addressable.
        for ref_key in catalog["capability_ref_index"]:
            self.assertIsNotNone(bcpc._parse_depends_on_ref(ref_key))

    def test_r4_project_id_addressability_is_recorded_for_every_capability(self) -> None:
        """project_id is derived from the filesystem, not declared in the
        file, so it cannot be validated the way id/kind/name are -- and
        excluding on it would be wrong, because same-project refs build the
        key identically on both sides and resolve fine regardless. Recorded
        rather than enforced; the real fleet is entirely addressable."""
        code, catalog = self._run()
        self.assertTrue(catalog["capabilities"])
        for cap in catalog["capabilities"]:
            self.assertTrue(cap["project_id_addressable"])
        self.assertEqual(catalog["counts"]["capabilities_with_unaddressable_project_id"], 0)

    # -----------------------------------------------------------------
    # R8: summarize_cli_inventory() can degrade like the other two.
    # -----------------------------------------------------------------

    def test_r8_garbage_inventory_file_is_schema_invalid_not_a_healthy_zero(self) -> None:
        """It was the only summarizer with no degrade path at all: an
        unrelated JSON object reported status:"ok", command_count:0, counted
        toward inventory_projects, exit 0 -- no signal to the operator that
        the file was garbage."""
        self._add_project("junkinv", inventory={"totally": "unrelated", "not": ["an", "inventory"]})
        code, catalog = self._run()
        self.assertEqual(code, 1)

        entry = next(p for p in catalog["projects"] if p["project_id"] == "junkinv")["sources"][
            "orca-cli-capability-inventory.json"
        ]
        self.assertEqual(entry["status"], "schema-invalid")
        self.assertIn("not a CLI capability inventory", entry["reason"])
        self.assertNotIn("command_count", entry)
        # Not counted as a contributing inventory, and no row published.
        self.assertEqual([i for i in catalog["cli_inventories"] if i["project_id"] == "junkinv"], [])
        self.assertEqual(catalog["counts"]["inventory_projects"], 1)  # alpha only
        degraded = [d for d in catalog["degraded"] if d["project_id"] == "junkinv"]
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0]["status"], "schema-invalid")

    def test_r8_minimal_but_real_inventory_still_passes(self) -> None:
        """The gate is deliberately permissive: any ONE recognized key is
        enough, so a genuinely empty inventory is not mistaken for garbage."""
        self._add_project("emptyinv", inventory={"schemaVersion": 1, "commandCount": 0})
        code, catalog = self._run()
        entry = next(p for p in catalog["projects"] if p["project_id"] == "emptyinv")["sources"][
            "orca-cli-capability-inventory.json"
        ]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["command_count"], 0)
        self.assertEqual(
            next(i for i in catalog["cli_inventories"] if i["project_id"] == "emptyinv")["command_count"], 0
        )

    # -----------------------------------------------------------------
    # P3-6 / P3-7: malformed sub-entries are counted, not silently skipped.
    # -----------------------------------------------------------------

    def test_p3_6_dropped_sub_entries_counted_and_source_marked_partial(self) -> None:
        self._add_project(
            "sloppy",
            caps={
                "schema_version": 1, "project": "sloppy",
                "capabilities": [
                    self._cap("good-one", "script", "g1.py"),
                    "not-even-a-dict",
                    {"kind": "script", "name": "no-id.py"},           # missing id
                    {"id": "no-name", "kind": "script"},               # missing name
                    self._cap("good-two", "skill", "g2-skill"),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)  # degraded[] is non-empty -> exit 1

        row = next(p for p in catalog["projects"] if p["project_id"] == "sloppy")
        entry = row["sources"]["reusable-capabilities.json"]
        self.assertEqual(entry["capability_count"], 2)
        self.assertEqual(entry["dropped_count"], 3)
        self.assertEqual(entry["status"], "partial")
        self.assertIn("dropped 3", entry["reason"])
        self.assertEqual(row["status"], "partial")

        # The two survivors are still fully catalogued -- "partial" degrades
        # the source without discarding what parsed.
        sloppy_caps = {c["id"] for c in catalog["capabilities"] if c["project_id"] == "sloppy"}
        self.assertEqual(sloppy_caps, {"good-one", "good-two"})

        # "partial" is wired through degraded[] like any other non-ok status.
        degraded = [d for d in catalog["degraded"] if d["project_id"] == "sloppy"]
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0]["status"], "partial")
        self.assertEqual(degraded[0]["source"], "reusable-capabilities.json")
        # ...and through the histogram and the contributing-project counts.
        self.assertEqual(catalog["counts"]["source_status_histogram"]["reusable-capabilities.json"]["partial"], 1)
        self.assertEqual(catalog["counts"]["capability_projects"], 2)  # alpha (ok) + sloppy (partial)

    def test_p3_7_invalid_kind_is_dropped_not_silently_indexed(self) -> None:
        self._add_project(
            "badkind",
            caps={
                "schema_version": 1, "project": "badkind",
                "capabilities": [
                    self._cap("legit", "config-pattern", "some-pattern"),
                    self._cap("bogus", "bogus-kind", "unreachable.py"),
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)

        entry = next(p for p in catalog["projects"] if p["project_id"] == "badkind")["sources"][
            "reusable-capabilities.json"
        ]
        self.assertEqual(entry["capability_count"], 1)
        self.assertEqual(entry["dropped_count"], 1)
        self.assertEqual(entry["status"], "partial")

        ids = {c["id"] for c in catalog["capabilities"] if c["project_id"] == "badkind"}
        self.assertEqual(ids, {"legit"})
        # The unreachable ref_key never entered the index. It could not have
        # been addressed anyway: _parse_depends_on_ref rejects any kind
        # outside KIND_VALUES, so "badkind:bogus-kind:unreachable.py" is an
        # unparseable ref, not a resolvable one.
        self.assertNotIn("badkind:bogus-kind:unreachable.py", catalog["capability_ref_index"])
        self.assertIsNone(bcpc._parse_depends_on_ref("badkind:bogus-kind:unreachable.py"))
        # A valid kind still parses, proving the enum -- not the whole
        # grammar -- is what rejected the above.
        self.assertIsNotNone(bcpc._parse_depends_on_ref("badkind:script:unreachable.py"))

    def test_p3_6_malformed_wiki_pages_counted_too(self) -> None:
        self._add_project(
            "badpages",
            wiki={
                "version": 1,
                "meta": {"content_version": 1},
                "project": {"path": str(self.projects_dir / "badpages")},
                "pages": [
                    {"id": "real", "title": "R", "path": "r.md", "summary": "s", "status": "live"},
                    "not-a-dict",
                    {"title": "no id here"},
                ],
                "links": [],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)
        entry = next(p for p in catalog["projects"] if p["project_id"] == "badpages")["sources"][
            "orca-context-wiki.json"
        ]
        self.assertEqual(entry["page_count"], 1)
        self.assertEqual(entry["dropped_count"], 2)
        self.assertEqual(entry["status"], "partial")
        kept = [p for p in catalog["wiki_pages"] if p["project_id"] == "badpages"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["global_id"], "badpages#page:real")

    # -----------------------------------------------------------------
    # P2-3: liveProbe.ok is nested, not a flat live_probe_ok key.
    # -----------------------------------------------------------------

    def test_p2_3_live_probe_ok_reads_the_nested_real_schema_field(self) -> None:
        self._add_project(
            "probed",
            inventory={
                "version": 1, "schemaVersion": 1, "commandCount": 1,
                "verificationCounts": {"live_read_only_verified": 1},
                "liveProbe": {"ok": False, "passed": 17, "failed": ["accounts"]},
                "commands": [{"command": "a", "summary": "s", "verification": "live_read_only_verified", "boundary": "b"}],
            },
        )
        code, catalog = self._run()
        inv = next(i for i in catalog["cli_inventories"] if i["project_id"] == "probed")
        # Before the fix this was None for every project on this machine,
        # because no real inventory has ever had a flat "live_probe_ok" key.
        self.assertIs(inv["live_probe_ok"], False)
        # alpha's inventory has no liveProbe block at all -> genuinely null.
        alpha_inv = next(i for i in catalog["cli_inventories"] if i["project_id"] == "alpha")
        self.assertIsNone(alpha_inv["live_probe_ok"])

    def test_p2_3_live_probe_ok_true_case(self) -> None:
        self._add_project(
            "probed_ok",
            inventory={
                "version": 1, "schemaVersion": 1, "commandCount": 0,
                "liveProbe": {"ok": True, "passed": 3, "failed": []},
                "commands": [],
            },
        )
        code, catalog = self._run()
        inv = next(i for i in catalog["cli_inventories"] if i["project_id"] == "probed_ok")
        self.assertIs(inv["live_probe_ok"], True)

    # -----------------------------------------------------------------
    # P3-4: "the file exists but is broken" is not "the project has no file".
    # -----------------------------------------------------------------

    def test_p3_4_broken_capabilities_file_is_not_reported_as_not_adopted(self) -> None:
        self._add_project("brokencaps", caps='{"schema_version": 1, "capabilities": [')  # truncated JSON
        self._add_project(
            "refbroken",
            caps={
                "schema_version": 1, "project": "refbroken",
                "capabilities": [self._cap("rb", "script", "rb.py", ["brokencaps:script:whatever.py"])],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)

        entry = next(p for p in catalog["projects"] if p["project_id"] == "brokencaps")["sources"][
            "reusable-capabilities.json"
        ]
        self.assertEqual(entry["status"], "parse-error")

        ref = next(r for r in catalog["unresolved_references"] if r["target_project_id"] == "brokencaps")
        # The old code misfiled this as "hasn't adopted the file yet", which
        # made SKILL.md's "resolves itself as adoption spreads" claim false:
        # adoption already happened, the file is just broken.
        self.assertEqual(ref["reason"], "capabilities-file-invalid")
        self.assertEqual(ref["target_project_state"], "enumerated-file-unreadable")
        # The contrasting case -- a genuinely un-adopted project still
        # reporting the old way -- is its own test below
        # (test_p3_4_unadopted_project_still_reports_not_adopted). It used to
        # be "asserted" here by computing "the first gamma ref, if any, else
        # None" over a fixture in which nothing references gamma at all, and
        # then asserting that it was None: an assertion that could not fail
        # whatever the module did.

    def test_p3_4_unadopted_project_still_reports_not_adopted(self) -> None:
        self._add_project(
            "refgamma",
            caps={
                "schema_version": 1, "project": "refgamma",
                "capabilities": [self._cap("rg", "script", "rg.py", ["gamma:script:nope.py"])],
            },
        )
        code, catalog = self._run()
        ref = next(r for r in catalog["unresolved_references"] if r["target_project_id"] == "gamma")
        self.assertEqual(ref["reason"], "project-has-no-capabilities-file")
        self.assertEqual(ref["target_project_state"], "enumerated-not-adopted")

    # -----------------------------------------------------------------
    # U2: the U+2028 sanitizer now covers the --json run summary on stdout.
    # -----------------------------------------------------------------

    def test_u2_json_summary_on_stdout_escapes_u2028(self) -> None:
        """The run summary echoes enumeration.orca_bin verbatim, so a path
        carrying U+2028 lands in stdout unless the sanitizer runs there
        too. Previously only the catalog FILE was sanitized."""
        sep_bin = self.tmp / f"fake{chr(0x2028)}orca.py"
        _write_fake_orca_v2(sep_bin, self.repo_payload, self.wt_payload)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bcpc.main(
                ["build", "--orca-bin", str(sep_bin), "--output", str(self.output_dir), "--json", "--force"]
            )
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("orca_bin", out)
        self.assertNotIn(chr(0x2028), out)
        self.assertIn("\\u2028", out)
        # Still valid JSON after escaping.
        self.assertEqual(json.loads(out)["enumeration"]["orca_bin"], str(sep_bin))

    def test_u2_fatal_payload_still_reaches_stderr_as_valid_json(self) -> None:
        """End-to-end exit-4 path: the payload is emitted through the
        sanitized boundary and still parses.

        The blocker sits INSIDE the pinned root, so the run clears the pin
        and genuinely reaches os.makedirs() -- a blocker outside it would
        stop earlier at exit 2 output_dir_not_permitted and never exercise
        the exit-4 boundary this test is about."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        blocker = self.output_dir / "not-a-dir"
        blocker.write_text("regular file, so makedirs() must fail", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = bcpc.main(
                ["build", "--orca-bin", str(self.orca_bin), "--output", str(blocker), "--json"]
            )
        err = buf.getvalue()
        self.assertEqual(code, 4)
        payload = json.loads(err)
        self.assertEqual(payload["reason"], "output_dir_uncreatable")
        self.assertIn("message", payload)

    # -----------------------------------------------------------------
    # R14: main()'s catch-all must not let KeyboardInterrupt / SystemExit
    # past the guarantee its own comment makes.
    # -----------------------------------------------------------------

    def test_r14_keyboard_interrupt_is_reported_then_reraised(self) -> None:
        """`except Exception` let both straight through, under a comment
        promising "this tool must never crash without an exit code"."""
        for exc_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exc=exc_type.__name__):
                def boom(_args):
                    raise exc_type("interrupted")

                buf = io.StringIO()
                with mock.patch.object(bcpc, "cmd_build", boom):
                    with contextlib.redirect_stderr(buf):
                        with self.assertRaises(exc_type):
                            bcpc.main(
                                ["build", "--orca-bin", str(self.orca_bin),
                                 "--output", str(self.output_dir), "--json"]
                            )
                payload = json.loads(buf.getvalue())
                self.assertEqual(payload["reason"], "unexpected_error")

    def test_r14_ordinary_exception_still_becomes_exit_4_not_a_traceback(self) -> None:
        def boom(_args):
            raise RuntimeError("ordinary failure")

        buf = io.StringIO()
        with mock.patch.object(bcpc, "cmd_build", boom):
            with contextlib.redirect_stderr(buf):
                code = bcpc.main(
                    ["build", "--orca-bin", str(self.orca_bin), "--output", str(self.output_dir), "--json"]
                )
        self.assertEqual(code, 4)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["reason"], "unexpected_error")
        self.assertEqual(payload["message"], "ordinary failure")

    # -----------------------------------------------------------------
    # R11 (round 1, closed in round 3): "contributed sources" must mean
    # contributed, not "has a wiki/ directory".
    # -----------------------------------------------------------------

    def test_r11_projects_with_sources_excludes_projects_that_contributed_nothing(self) -> None:
        """projects_with_sources used to be
        `sum(project_status_histogram[ok|partial])`, which counts a project
        whose wiki/ exists but holds none of the three allow-listed files:
        nothing was wrong with it, so its status is "ok", so it was counted
        as having "contributed sources" when all three of its sources are
        "absent". The human summary line
        ("N of M enumerated project paths contributed sources") was inflated
        by exactly those projects. This item had ZERO test coverage: a
        mutant pinning the count to the constant 0 survived the whole suite.
        """
        for name in ("empty1", "empty2", "empty3"):
            project = self._add_project(name)
            (project / "wiki" / "README.md").write_text("not an allow-listed file\n", encoding="utf-8")

        code, catalog = self._run()
        self.assertEqual(code, 0)
        counts = catalog["counts"]

        # 4 base fixture targets + the 3 new ones.
        self.assertEqual(counts["scan_targets"], 7)
        # All three new projects are "ok" -- nothing is wrong with them...
        self.assertEqual(counts["project_status_histogram"]["ok"], 5)
        for name in ("empty1", "empty2", "empty3"):
            row = next(p for p in catalog["projects"] if p["project_id"] == name)
            self.assertEqual(row["status"], "ok")
            self.assertEqual(
                {entry["status"] for entry in row["sources"].values()}, {"absent"}
            )
        # ...but only alpha and beta actually contributed anything.
        self.assertEqual(counts["projects_with_sources"], 2)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bcpc._print_human_summary(catalog, True, self.output_dir / bcpc.CATALOG_NAME)
        self.assertIn("catalog: 2 of 7 enumerated project paths contributed sources", buf.getvalue())

    def test_r11_a_partial_source_still_counts_as_contributing(self) -> None:
        """The contrast case, so the fix is not "count only ok". A file with
        one good and one malformed capability is "partial": its survivors
        are kept, so the project really did contribute."""
        self._add_project(
            "halfgood",
            caps={
                "schema_version": 1, "project": "halfgood",
                "capabilities": [self._cap("good-one", "script", "g.py"), "not a dict at all"],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 1)  # a dropped entry degrades the source
        row = next(p for p in catalog["projects"] if p["project_id"] == "halfgood")
        self.assertEqual(row["sources"]["reusable-capabilities.json"]["status"], "partial")
        # alpha + beta + halfgood
        self.assertEqual(catalog["counts"]["projects_with_sources"], 3)

    # -----------------------------------------------------------------
    # R14 (round 1, closed in round 3): redundant_spelling on ALL THREE
    # parsed depends_on arms, not two of them.
    # -----------------------------------------------------------------

    def test_redundant_spelling_is_attached_on_all_three_parsed_arms(self) -> None:
        """`redundant = scope == "cross-project" and target == own project`
        was computed once and applied on the resolved and self-reference
        arms only; the unresolved arm appended a bare 4-key dict. A consumer
        could not tell "not redundant" from "this arm never computes it".
        Untested until now: a mutant ADDING the field to the unresolved arm
        survived the whole suite, and so did one removing it."""
        self._add_project(
            "selfp",
            caps={
                "schema_version": 1, "project": "selfp",
                "capabilities": [
                    self._cap(
                        "a", "script", "real.py",
                        [
                            "selfp:script:nope.py",   # unresolved, redundantly spelled
                            "selfp:script:real.py",   # self-reference, redundantly spelled
                            "script:ghost.py",        # unresolved, NOT redundantly spelled
                        ],
                    ),
                    self._cap("b", "script", "tgt.py"),
                    self._cap("c", "script", "srcc.py", ["selfp:script:tgt.py"]),  # resolved, redundant
                ],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)

        cap_a = next(c for c in catalog["capabilities"] if c["id"] == "a")
        by_raw = {d["raw"]: d for d in cap_a["depends_on"]}
        self.assertEqual(by_raw["selfp:script:nope.py"]["state"], "unresolved")
        self.assertTrue(by_raw["selfp:script:nope.py"]["redundant_spelling"])
        self.assertEqual(by_raw["selfp:script:real.py"]["state"], "self-reference")
        self.assertTrue(by_raw["selfp:script:real.py"]["redundant_spelling"])
        # The non-redundant spelling of an equally-unresolved ref must NOT
        # carry the flag -- otherwise the field would be meaningless.
        self.assertEqual(by_raw["script:ghost.py"]["state"], "unresolved")
        self.assertNotIn("redundant_spelling", by_raw["script:ghost.py"])

        cap_c = next(c for c in catalog["capabilities"] if c["id"] == "c")
        dep = cap_c["depends_on"][0]
        self.assertEqual(dep["state"], "resolved")
        self.assertTrue(dep["redundant_spelling"])

        # All three arms agree on the field's meaning.
        redundant_states = {
            d["state"]
            for c in catalog["capabilities"]
            for d in c["depends_on"]
            if d.get("redundant_spelling")
        }
        self.assertEqual(redundant_states, {"resolved", "self-reference", "unresolved"})

    # -----------------------------------------------------------------
    # F2 (round 2): page/capability global_id disjointness is MEASURED,
    # not merely argued from ID_RE.
    # -----------------------------------------------------------------

    def test_f2_cross_namespace_global_id_collision_is_measured_and_published(self) -> None:
        """ID_RE closes the `id` axis; nothing closes the `project_id` axis,
        because project_id is derived from the filesystem and this tool must
        not refuse to catalog real content over how a directory is named. A
        project directory carrying BOTH '#' and ':' can therefore still mint
        a capability global_id byte-identical to some page's:

            project "foo#page:x" + capability id "bar" -> "foo#page:x#bar"
            project "foo"        + page id "x#bar"     -> "foo#page:x#bar"

        The old comment at the page mint site claimed no such collision "is
        constructible". It is. The claim is now replaced by a measurement:
        the overlap is computed for real and published."""
        self._add_project(
            "foo#page:x",
            caps={
                "schema_version": 1, "project": "foo#page:x",
                "capabilities": [self._cap("bar", "skill", "s")],
            },
        )
        self._add_project(
            "foo",
            wiki={
                "version": 1, "meta": {"content_version": 1},
                "project": {"path": str(self.projects_dir / "foo")},
                "pages": [{"id": "x#bar", "title": "T", "path": "h.md", "summary": "s", "status": "live"}],
                "links": [],
            },
        )
        code, catalog = self._run()
        self.assertEqual(code, 0)  # pure reporting: nothing degrades, nothing is dropped

        page_gids = {p["global_id"] for p in catalog["wiki_pages"]}
        cap_gids = {c["global_id"] for c in catalog["capabilities"]}
        overlap = page_gids & cap_gids
        self.assertEqual(overlap, {"foo#page:x#bar"})
        # ...and the catalog says so, instead of a comment promising it
        # cannot happen.
        self.assertEqual(catalog["cross_namespace_global_id_collisions"], ["foo#page:x#bar"])
        self.assertEqual(catalog["counts"]["cross_namespace_global_id_collisions"], 1)
        # The partial early-warning signal fires on the same input.
        colliding_cap = next(c for c in catalog["capabilities"] if c["global_id"] == "foo#page:x#bar")
        self.assertFalse(colliding_cap["project_id_addressable"])
        self.assertGreaterEqual(catalog["counts"]["capabilities_with_unaddressable_project_id"], 1)
        # Neither per-namespace duplicate detector can see it -- which is
        # exactly why the cross-namespace measurement has to exist.
        self.assertFalse(colliding_cap["duplicate_global_id"])
        self.assertEqual(catalog["ambiguous_page_global_ids"], [])

    def test_f2_a_clean_fleet_publishes_an_empty_collision_list(self) -> None:
        """The measurement must be able to say "none", or a green result
        proves nothing."""
        code, catalog = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(catalog["cross_namespace_global_id_collisions"], [])
        self.assertEqual(catalog["counts"]["cross_namespace_global_id_collisions"], 0)
        page_gids = {p["global_id"] for p in catalog["wiki_pages"]}
        cap_gids = {c["global_id"] for c in catalog["capabilities"]}
        self.assertTrue(page_gids)
        self.assertTrue(cap_gids)
        self.assertEqual(page_gids & cap_gids, set())


# ---------------------------------------------------------------------------
# U2 unit-level: the sanitizer itself.
# ---------------------------------------------------------------------------


class SanitizerTests(unittest.TestCase):
    def test_escapes_both_separators_and_leaves_everything_else(self) -> None:
        raw = f'{{"a": "x{chr(0x2028)}y{chr(0x2029)}z", "b": "中文"}}'
        out = bcpc._sanitize_line_separators(raw)
        self.assertNotIn(chr(0x2028), out)
        self.assertNotIn(chr(0x2029), out)
        self.assertIn("\\u2028", out)
        self.assertIn("\\u2029", out)
        self.assertEqual(json.loads(out)["a"], f"x{chr(0x2028)}y{chr(0x2029)}z")
        self.assertEqual(json.loads(out)["b"], "中文")

    def test_catalog_encoder_uses_the_shared_helper(self) -> None:
        payload = bcpc._encode_catalog({"k": f"a{chr(0x2028)}b"})
        self.assertNotIn(chr(0x2028).encode("utf-8"), payload)
        self.assertIn(b"\\u2028", payload)

    def test_fatal_stderr_payload_boundary_is_sanitized(self) -> None:
        """Third json.dumps output boundary: the exit-4 payload on stderr.

        Driven directly rather than through a real fatal path on purpose --
        str(OSError) renders its filename with repr(), which already escapes
        U+2028 to literal text, so no CURRENT fatal path is known to carry a
        raw separator. This is boundary defense-in-depth: `message` is
        str(exc) for arbitrary exceptions (see main()'s "unexpected_error"
        catch-all), and the boundary must not depend on which exception type
        happens to reach it.
        """
        args = argparse.Namespace(quiet=False, json=True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = bcpc._emit_error(args, 4, "unexpected_error", f"boom{chr(0x2028)}bang{chr(0x2029)}")
        err = buf.getvalue()
        self.assertEqual(code, 4)
        self.assertNotIn(chr(0x2028), err)
        self.assertNotIn(chr(0x2029), err)
        self.assertIn("\\u2028", err)
        self.assertIn("\\u2029", err)
        payload = json.loads(err)
        self.assertEqual(payload["reason"], "unexpected_error")
        self.assertEqual(payload["message"], f"boom{chr(0x2028)}bang{chr(0x2029)}")


# ---------------------------------------------------------------------------
# Isolation P2-1: --output is a PINNED write boundary.
# ---------------------------------------------------------------------------


def _empty_fleet_stub(path: Path) -> None:
    _write_fake_orca_v2(
        path,
        {"id": "x", "ok": True, "result": {"repos": []}, "_meta": {}},
        {"id": "x", "ok": True, "result": {"worktrees": [], "totalCount": 0, "truncated": False}, "_meta": {}},
    )


class OutputPinningTests(unittest.TestCase):
    """`write_only_within()` is parameterized ON the chosen --output dir, so
    it structurally cannot reject an escaping --output -- it only ever
    confines the catalog FILE to whatever directory was named. The pin lives
    in cmd_build() instead, and must run BEFORE os.makedirs()."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bcpc-pin-"))
        self.orca_bin = self.tmp / "fake_orca.py"
        _empty_fleet_stub(self.orca_bin)
        self._saved_default = bcpc.DEFAULT_OUTPUT_DIR

    def tearDown(self) -> None:
        bcpc.DEFAULT_OUTPUT_DIR = self._saved_default
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, output: Path | str, extra: list[str] | None = None) -> tuple[int, str | None]:
        argv = ["build", "--orca-bin", str(self.orca_bin), "--output", str(output), "--json"]
        if extra:
            argv += extra
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with contextlib.redirect_stdout(io.StringIO()):
                code = bcpc.main(argv)
        text = buf.getvalue().strip()
        reason = json.loads(text)["reason"] if text else None
        return code, reason

    def test_rejects_output_outside_the_real_default_dir(self) -> None:
        escape = self.tmp / "escape-out"
        code, reason = self._run(escape)
        self.assertEqual(code, 2)
        self.assertEqual(reason, "output_dir_not_permitted")
        # And nothing was created: the pin runs before os.makedirs().
        self.assertFalse(escape.exists())

    def test_rejects_output_pointed_inside_a_project_tree(self) -> None:
        """The concrete risk the pin closes: aiming writes at a live
        project's git tree, which the old code accepted."""
        project = self.tmp / "projects"
        build_fixture_tree(project)
        before = _snapshot(project)
        code, reason = self._run(project / "alpha" / "wiki")
        self.assertEqual(code, 2)
        self.assertEqual(reason, "output_dir_not_permitted")
        self.assertEqual(before, _snapshot(project))

    def test_no_output_override_flag_exists_at_all(self) -> None:
        """The pin briefly had a hidden --i-understand-output-override
        escape hatch. It reopened exactly the hole the pin closes: with it,
        --output <a live project's git tree> wrote catalog.json inside that
        tree at exit 0. It was deleted, not merely hidden -- this test is
        what keeps it deleted, and what makes the module docstring's
        UNCONDITIONAL pin claim literally true.

        Three independent proofs: argparse rejects the flag, no action in
        the parser carries that dest, and the write it used to permit is
        now refused."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as caught:
                bcpc.main(["build", "--orca-bin", str(self.orca_bin), "--i-understand-output-override"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("unrecognized arguments", buf.getvalue())

        parsed = bcpc.build_parser().parse_args(["build"])
        self.assertFalse(hasattr(parsed, "i_understand_output_override"))
        self.assertTrue(hasattr(parsed, "output"))

        # ...and the concrete write it used to allow is now refused.
        project = self.tmp / "override-projects"
        build_fixture_tree(project)
        before = _snapshot(project)
        code, reason = self._run(project / "alpha" / "wiki")
        self.assertEqual(code, 2)
        self.assertEqual(reason, "output_dir_not_permitted")
        self.assertEqual(before, _snapshot(project))
        self.assertFalse((project / "alpha" / "wiki" / bcpc.CATALOG_NAME).exists())

    def test_default_dir_itself_and_descendants_are_accepted(self) -> None:
        pinned = self.tmp / "pinned"
        bcpc.DEFAULT_OUTPUT_DIR = pinned
        self.assertEqual(self._run(pinned)[0], 0)
        self.assertEqual(self._run(pinned / "sub" / "deeper")[0], 0)
        self.assertTrue((pinned / "sub" / "deeper" / bcpc.CATALOG_NAME).is_file())
        # A sibling of the pinned root is still refused.
        code, reason = self._run(self.tmp / "not-pinned")
        self.assertEqual(code, 2)
        self.assertEqual(reason, "output_dir_not_permitted")

    def test_dotdot_escape_from_pinned_root_rejected_post_resolve(self) -> None:
        pinned = self.tmp / "pinned2"
        pinned.mkdir()
        bcpc.DEFAULT_OUTPUT_DIR = pinned
        # Path.absolute() does not normalize '..', so this clears the
        # lexical check and must be caught by the resolve pass.
        code, reason = self._run(pinned / ".." / "sneaky")
        self.assertEqual(code, 2)
        self.assertEqual(reason, "output_dir_not_permitted")
        self.assertFalse((self.tmp / "sneaky").exists())

    def test_output_dir_that_is_itself_a_symlink_is_rejected(self) -> None:
        pinned = self.tmp / "pinned3"
        real = pinned / "real"
        real.mkdir(parents=True)
        link = pinned / "link"
        os.symlink(str(real), str(link))
        bcpc.DEFAULT_OUTPUT_DIR = pinned
        # Lexically and post-resolve this IS inside the pinned root, so it
        # clears the pin -- the symlink check is the thing that stops it.
        # write_only_within() never examines the root itself, by design.
        code, reason = self._run(link)
        self.assertEqual(code, 2)
        self.assertEqual(reason, "output_dir_is_symlink")
        self.assertFalse((real / bcpc.CATALOG_NAME).exists())

    def test_symlinked_pinned_root_itself_is_still_rejected(self) -> None:
        """The symlink check must not depend on the pin having passed for
        some particular reason: a symlink that IS the pinned root clears the
        containment checks (it is trivially inside itself) and only the
        explicit islink() test stops it."""
        real = self.tmp / "sym-real"
        real.mkdir()
        link = self.tmp / "sym-link"
        os.symlink(str(real), str(link))
        bcpc.DEFAULT_OUTPUT_DIR = link
        code, reason = self._run(link)
        self.assertEqual(code, 2)
        self.assertEqual(reason, "output_dir_is_symlink")
        self.assertFalse((real / bcpc.CATALOG_NAME).exists())

    def test_help_advertises_output_and_no_override(self) -> None:
        """SKILL.md documents the pin as absolute; --help must not offer
        anything that contradicts it."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                bcpc.main(["build", "--help"])
        help_text = buf.getvalue()
        self.assertIn("--output", help_text)
        self.assertNotIn("override", help_text)


# ---------------------------------------------------------------------------
# Isolation P3-8 / P3-3: tmp-file cleanup and the previous-catalog read.
# ---------------------------------------------------------------------------


class AtomicWriteCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bcpc-atomic-"))
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tmp_leftovers(self) -> list[str]:
        return sorted(p.name for p in self.out.iterdir() if p.name.startswith(".catalog.json.tmp-"))

    def test_p3_8_tmp_removed_when_os_replace_itself_fails(self) -> None:
        """The cleanup bracket used to stop before os.replace(), so a failing
        replace orphaned a .catalog.json.tmp-<pid>-<ms> every attempt."""
        final_path = self.out / "catalog.json"
        final_path.mkdir()  # destination is a directory -> os.replace raises
        with self.assertRaises(OSError):
            bcpc.atomic_write_within(self.out, final_path, b'{"ok": true}')
        self.assertEqual(self._tmp_leftovers(), [])
        # Repeated failures must not accumulate junk either.
        for _ in range(3):
            with self.assertRaises(OSError):
                bcpc.atomic_write_within(self.out, final_path, b'{"ok": true}')
        self.assertEqual(self._tmp_leftovers(), [])

    def test_p3_8_tmp_removed_when_the_write_fails(self) -> None:
        final_path = self.out / "catalog.json"
        with self.assertRaises(TypeError):
            bcpc.atomic_write_within(self.out, final_path, "not bytes")  # type: ignore[arg-type]
        self.assertEqual(self._tmp_leftovers(), [])

    def test_p3_8_success_path_still_leaves_no_tmp_behind(self) -> None:
        final_path = self.out / "catalog.json"
        bcpc.atomic_write_within(self.out, final_path, b'{"ok": true}')
        self.assertEqual(final_path.read_bytes(), b'{"ok": true}')
        self.assertEqual(self._tmp_leftovers(), [])

    def test_f9_tmp_file_open_flags_include_o_nofollow_and_o_excl(self) -> None:
        """Round 1 credited "O_NOFOLLOW on all four opens" as a verified
        property, but nothing pinned it at THIS call site -- a mutant
        dropping it survived the suite (dropping O_EXCL was killed).

        Honest scope: O_NOFOLLOW is unreachable-in-effect here, because
        O_CREAT|O_EXCL already fails EEXIST on a symlink whether or not
        O_NOFOLLOW is set (verified: both flag sets raise FileExistsError
        errno 17 on a pre-planted symlink). So this test pins the FLAG, not
        a behaviour difference -- it exists so the documented property stops
        being unpinned, and the symlink assertion below records which flag
        is actually doing the work."""
        final_path = self.out / "catalog.json"
        seen: list[int] = []
        real_open = os.open

        def spy(path, flags, *args):
            seen.append(flags)
            return real_open(path, flags, *args)

        with mock.patch.object(bcpc.os, "open", spy):
            bcpc.atomic_write_within(self.out, final_path, b'{"ok": true}')
        # Two opens: the tmp file, then the O_RDONLY directory handle used
        # for the best-effort directory fsync. Only the first is the write.
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0] & os.O_NOFOLLOW)
        self.assertTrue(seen[0] & os.O_EXCL)
        self.assertTrue(seen[0] & os.O_CREAT)
        self.assertTrue(seen[0] & os.O_WRONLY)

        # And the behaviour O_EXCL alone already guarantees: a symlink
        # pre-planted at the tmp path is refused, never followed.
        victim = self.tmp / "victim"
        victim.write_text("original", encoding="utf-8")
        planted = self.out / ".catalog.json.tmp-plant"
        os.symlink(str(victim), str(planted))
        with self.assertRaises(FileExistsError):
            os.open(str(planted), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with self.assertRaises(FileExistsError):
            os.open(str(planted), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.assertEqual(victim.read_text(encoding="utf-8"), "original")


class PreviousCatalogReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bcpc-prev-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_p3_3_symlinked_previous_catalog_is_not_followed(self) -> None:
        """This read happens after the whole fleet scan, seconds past the
        write guard -- a symlink swapped in during that window used to be
        followed. O_NOFOLLOW makes it indistinguishable from "no previous
        catalog", which only ever forces a rebuild."""
        victim = self.tmp / "victim.json"
        victim.write_text('{"content_fingerprint": "stolen"}', encoding="utf-8")
        link = self.tmp / "catalog.json"
        os.symlink(str(victim), str(link))
        self.assertIsNone(bcpc._read_previous_catalog(link))

    def test_p3_3_regular_previous_catalog_still_read(self) -> None:
        real = self.tmp / "catalog.json"
        real.write_text('{"content_fingerprint": "abc"}', encoding="utf-8")
        doc = bcpc._read_previous_catalog(real)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["content_fingerprint"], "abc")

    def test_p3_3_missing_unparseable_and_non_object_all_degrade_to_none(self) -> None:
        self.assertIsNone(bcpc._read_previous_catalog(self.tmp / "nope.json"))
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertIsNone(bcpc._read_previous_catalog(bad))
        arr = self.tmp / "arr.json"
        arr.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(bcpc._read_previous_catalog(arr))
        a_dir = self.tmp / "adir.json"
        a_dir.mkdir()
        self.assertIsNone(bcpc._read_previous_catalog(a_dir))

    # F4 (round 3): these two are named so that unittest's lexicographic
    # method ordering runs the flag spy FIRST.
    #
    # They used to be `test_r2_fifo_...` and `test_r2_o_nonblock_...`, i.e.
    # "f" before "o", which put the blocking test first. That mattered: the
    # FIFO test called _read_previous_catalog() IN-PROCESS, so a regression
    # that drops O_NONBLOCK does not fail it -- it wedges the whole test
    # process in the kernel forever, the `finally: fifo.unlink()` never
    # runs, and the flag spy that WOULD have failed fast never gets to run
    # at all. CI stalls instead of reporting. Both halves of that are fixed:
    # the spy sorts first, and the FIFO call now happens in a subprocess
    # under a hard timeout, so the timeout is a real deadlock detector
    # instead of an assertion sitting unreachably behind the hang.

    def test_r2_a_o_nonblock_is_actually_on_the_open_flags(self) -> None:
        """The genuine, fail-fast regression guard for R2.

        Asserts the production call really requests O_NONBLOCK (and
        O_NOFOLLOW) by spying on the flags, which needs no FIFO, cannot
        block, and cannot pass by accident on a fast machine. If the flag is
        ever dropped, THIS is the test that reports it."""
        real = self.tmp / "catalog.json"
        real.write_text('{"content_fingerprint": "abc"}', encoding="utf-8")
        seen: list[int] = []
        real_open = os.open

        def spy(path, flags, *args):
            seen.append(flags)
            return real_open(path, flags, *args)

        with mock.patch.object(bcpc.os, "open", spy):
            self.assertIsNotNone(bcpc._read_previous_catalog(real))
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0] & os.O_NONBLOCK)
        self.assertTrue(seen[0] & os.O_NOFOLLOW)

    def test_r2_b_fifo_previous_catalog_returns_instead_of_hanging_forever(self) -> None:
        """A FIFO planted at the catalog path used to wedge the process in
        the kernel, inside os.open() itself, holding .catalog.lock -- so
        every subsequent run was refused `lock_held` until the 300s
        staleness window, and then wedged in turn. Measured before the fix:
        still blocked after 8s, SIGKILL required.

        O_NOFOLLOW does not help here: a FIFO is not a symlink, it is the
        real file at that path. The S_ISREG guard does not help either -- it
        is downstream of the blocking call and could never be reached.
        O_NONBLOCK is what makes the open return, after which S_ISREG
        rejects the FIFO the way it always meant to.

        The call runs in a SUBPROCESS under a 20s hard timeout, which is
        what makes that budget an actual deadlock detector: on a regression
        the child wedges, subprocess.run raises TimeoutExpired and kills it,
        and this test FAILS with a clear message. An in-process call could
        not do that -- any timing assertion after it is unreachable exactly
        when the bug it is meant to catch is present."""
        fifo = self.tmp / "catalog.json"
        os.mkfifo(str(fifo))
        program = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('bcpc', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "import pathlib\n"
            "print('RESULT=' + repr(m._read_previous_catalog(pathlib.Path(sys.argv[2]))))\n"
        )
        try:
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    [sys.executable, "-c", program, str(Path(bcpc.__file__).resolve()), str(fifo)],
                    capture_output=True, timeout=20,
                )
            except subprocess.TimeoutExpired:
                self.fail(
                    "_read_previous_catalog() did not return within 20s on a FIFO -- "
                    "O_NONBLOCK regression (the open blocked in the kernel)"
                )
            elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            self.assertIn("RESULT=None", proc.stdout.decode("utf-8", "replace"))
            # Perf is NOT what this test guarantees -- the timeout above is.
            # This is only a sanity note that the fixed path is nowhere near
            # the budget.
            self.assertLess(elapsed, 20.0)
            # Still a FIFO: read-only, and it was rejected rather than drained.
            self.assertTrue(stat.S_ISFIFO(os.stat(str(fifo)).st_mode))
        finally:
            fifo.unlink()


# ---------------------------------------------------------------------------
# Isolation P2-2: no .pyc is written into a content-bearing project tree.
# ---------------------------------------------------------------------------


class NoBytecodeInProjectTreeTests(unittest.TestCase):
    def test_p2_2_documented_invocation_writes_no_pycache_next_to_the_script(self) -> None:
        """The sibling `import validate_reusable_capabilities` used to drop a
        __pycache__/*.pyc into orca-context-bridge/scripts/ -- inside one of
        the very projects this aggregator reads. Run with an EMPTY
        PYTHONDONTWRITEBYTECODE so the guarantee is proven to come from
        sys.dont_write_bytecode in the source, not from the environment."""
        scripts_dir = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as staging:
            staged = Path(staging) / "scripts"
            shutil.copytree(str(scripts_dir), str(staged), ignore=shutil.ignore_patterns("__pycache__"))
            self.assertFalse((staged / "__pycache__").exists())
            env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
            proc = subprocess.run(
                [sys.executable, str(staged / "build_cross_project_catalog.py"), "build", "--help"],
                cwd=staging, env=env, capture_output=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            self.assertFalse(
                (staged / "__pycache__").exists(),
                "importing validate_reusable_capabilities wrote bytecode into the scripts/ dir",
            )

    def test_p2_2_module_sets_dont_write_bytecode(self) -> None:
        source = (Path(bcpc.__file__)).read_text(encoding="utf-8")
        marker = "sys.dont_write_bytecode = True"
        self.assertIn(marker, source)
        # It must come BEFORE the sibling import, or it is too late.
        # (An older comment here claimed this phrase "also appears in the
        # module docstring" and that the index arithmetic worked around
        # that. It does not appear there -- the marker occurs exactly once
        # in the whole file -- so the plain index() below is unambiguous.)
        self.assertEqual(source.count(marker), 1)
        real_import = "from validate_reusable_capabilities import ID_MAX_LEN, ID_RE, derive_expected_project_id  # noqa"
        self.assertLess(source.index(marker), source.index(real_import))


if __name__ == "__main__":
    unittest.main()
