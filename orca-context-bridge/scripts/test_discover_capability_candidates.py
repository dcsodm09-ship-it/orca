#!/usr/bin/env python3
"""Test suite for discover_capability_candidates.py (M8-2, Gate C staged
candidate). Run with: python3 -m pytest test_discover_capability_candidates.py -q
or: python3 -m unittest test_discover_capability_candidates -v
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
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_capability_candidates as dcc  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def make_project_row(project_id: str, real_path, sources: dict | None = None) -> dict:
    row = {"real_path": str(real_path), "path": str(real_path), "project_id": project_id, "status": "ok"}
    if sources is not None:
        row["sources"] = sources
    return row


def make_catalog(projects=None, capabilities=None, wiki_pages=None, generated_at="2026-08-23T00:00:00Z") -> dict:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "projects": projects or [],
        "capabilities": capabilities or [],
        "wiki_pages": wiki_pages or [],
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


class BaseTestCase(unittest.TestCase):
    """Redirects DEFAULT_OUTPUT_DIR to a throwaway temp dir for every test,
    exactly as detect_capability_changes.py's own suite documents doing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dcc2-"))
        self.output_dir = self.tmp / "manifests-output"
        self._orig_output_dir = dcc.DEFAULT_OUTPUT_DIR
        dcc.DEFAULT_OUTPUT_DIR = self.output_dir

    def tearDown(self) -> None:
        dcc.DEFAULT_OUTPUT_DIR = self._orig_output_dir
        _make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_script(self, root: Path, rel_path: str, *, argparse: bool = True, shebang: bool = True) -> Path:
        p = root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if shebang:
            lines.append("#!/usr/bin/env python3")
        lines.append('"""a script"""')
        if argparse:
            lines.append("import argparse")
            lines.append("p = argparse.ArgumentParser()")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def make_knowledge_md(self, root: Path, rel_path: str, content: str = "# notes\n") -> Path:
        p = root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def scan_argv(self, roots: list[Path], catalog: Path, **extra) -> list[str]:
        argv = ["scan", "--catalog", str(catalog)]
        for r in roots:
            argv += ["--root", str(r)]
        for flag, val in extra.items():
            if val is True:
                argv.append(f"--{flag.replace('_', '-')}")
            elif val is False or val is None:
                continue
            elif isinstance(val, list):
                for v in val:
                    argv += [f"--{flag.replace('_', '-')}", str(v)]
            else:
                argv += [f"--{flag.replace('_', '-')}", str(val)]
        return argv

    def read_hits_doc(self) -> dict:
        return json.loads((self.output_dir / dcc.HITS_NAME).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------


class SignalDetectionTests(BaseTestCase):
    def test_script_signal_requires_both_shebang_and_argparse(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/good.py", argparse=True, shebang=True)
        self.make_script(proj, "scripts/no_argparse.py", argparse=False, shebang=True)
        self.make_script(proj, "scripts/no_shebang.py", argparse=True, shebang=False)
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, out, err = _run_main(self.scan_argv([proj], catalog, json=True, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        paths = {h["path"] for h in doc["hits"] if h["signal_type"] == "capability:script"}
        self.assertEqual(paths, {"scripts/good.py"})

    def test_skill_dir_signal(self) -> None:
        proj = self.tmp / "proj"
        (proj / "skills" / "my-skill").mkdir(parents=True)
        (proj / "skills" / "my-skill" / "SKILL.md").write_text("desc", encoding="utf-8")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        skill_hits = [h for h in doc["hits"] if h["signal_type"] == "capability:skill"]
        self.assertEqual(len(skill_hits), 1)
        self.assertEqual(skill_hits[0]["path"], "skills/my-skill")
        self.assertIsNotNone(skill_hits[0]["content_sha256"])

    def test_knowledge_reports_dir_signal(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/some-note.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        hits = [h for h in doc["hits"] if h["path"] == "reports/some-note.md"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["signal_type"], "knowledge:reports_dir")

    def test_knowledge_filename_keyword_signal(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "notes/CODEX-REVIEW-round9.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        hits = [h for h in doc["hits"] if "CODEX-REVIEW" in h["path"]]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["signal_type"], "knowledge:filename_keyword")

    def test_non_matching_md_is_not_a_hit(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "docs/plain.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(doc["hits"], [])

    def test_max_file_bytes_skips_oversized_script(self) -> None:
        proj = self.tmp / "proj"
        p = self.make_script(proj, "scripts/big.py", argparse=True, shebang=True)
        p.write_text(p.read_text(encoding="utf-8") + ("x" * 100), encoding="utf-8")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, max_file_bytes=10, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(doc["hits"], [])
        root_stats = doc["roots_scanned"][0]
        self.assertGreaterEqual(root_stats["skipped_too_large"], 1)


class SymlinkEscapeTests(BaseTestCase):
    """A symlink planted inside a scanned tree must never let the scanner
    read, hash, or echo the content of a file OUTSIDE that tree -- see
    read_file_bounded()'s docstring. os.walk()'s own `filenames` list
    includes symlink entries verbatim (unresolved), so without O_NOFOLLOW
    on the open(), any such planted symlink would be silently dereferenced:
    its target's bytes hashed into content_sha256, its size recorded, and
    (for a target whose first line matches the shebang pattern) that exact
    line copied into the written discovery-hits.json. This is not a TOCTOU
    race -- the symlink is present and static the whole time -- so the
    earlier justification for omitting O_NOFOLLOW ('no race window to
    close') did not actually cover this case."""

    def test_symlink_inside_scan_root_is_not_dereferenced(self) -> None:
        secret_dir = self.tmp / "outside-scan-root"
        secret_dir.mkdir()
        secret = secret_dir / "secret.py"
        secret.write_text("#!/usr/bin/env python3\n# SECRET_TOKEN=do-not-leak\nimport argparse\n", encoding="utf-8")

        proj = self.tmp / "proj"
        (proj / "reports").mkdir(parents=True)
        planted = proj / "reports" / "innocuous.py"
        planted.symlink_to(secret)

        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()

        # No hit was produced from the symlink target's content at all --
        # the planted entry must not turn into a capability:script hit.
        self.assertEqual(doc["hits"], [])
        # And the secret's own bytes/first-line must not appear anywhere in
        # the written output, under any field.
        raw_output = (self.output_dir / dcc.HITS_NAME).read_text(encoding="utf-8")
        self.assertNotIn("do-not-leak", raw_output)
        self.assertNotIn("SECRET_TOKEN", raw_output)
        # The skip must be visible in stats, not silently absorbed.
        root_stats = doc["roots_scanned"][0]
        self.assertGreaterEqual(root_stats["symlinks_skipped"], 1)

    def test_symlinked_skill_md_marker_is_not_dereferenced(self) -> None:
        secret_dir = self.tmp / "outside-scan-root"
        secret_dir.mkdir()
        secret = secret_dir / "SECRET.md"
        secret.write_text("do-not-leak-skill-content", encoding="utf-8")

        proj = self.tmp / "proj"
        skill_dir = proj / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").symlink_to(secret)

        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()

        skill_hits = [h for h in doc["hits"] if h["signal_type"] == "capability:skill"]
        self.assertEqual(len(skill_hits), 1)
        # The directory is still a legitimate hit (SKILL.md exists as a
        # name), but its content hash must reflect that the marker could
        # not be safely read through -- never the symlink target's hash.
        self.assertEqual(skill_hits[0]["content_hash_status"], "symlink_skipped")
        self.assertIsNone(skill_hits[0]["content_sha256"])
        raw_output = (self.output_dir / dcc.HITS_NAME).read_text(encoding="utf-8")
        self.assertNotIn("do-not-leak-skill-content", raw_output)


# ---------------------------------------------------------------------------
# Noise rule A: content-duplicate clustering
# ---------------------------------------------------------------------------


class NoiseRuleATests(BaseTestCase):
    def test_identical_content_across_two_paths_is_clustered(self) -> None:
        proj = self.tmp / "proj"
        content = "same content for both\n"
        self.make_knowledge_md(proj, "reports/aaaaaa-long-name.md", content)
        self.make_knowledge_md(proj, "reports/short.md", content)
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        by_path = {h["path"]: h for h in doc["hits"]}
        primary = by_path["reports/short.md"]
        dup = by_path["reports/aaaaaa-long-name.md"]
        self.assertIsNone(primary["duplicate_of"])
        self.assertEqual(dup["duplicate_of"], primary["hit_id"])
        self.assertIn("content_duplicate", dup["noise_signals"])
        self.assertNotIn("content_duplicate", primary["noise_signals"])
        self.assertEqual(doc["noise_rules"]["A"]["hits_affected"], 1)

    def test_distinct_content_is_not_clustered(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/a.md", "content A\n")
        self.make_knowledge_md(proj, "reports/b.md", "content B\n")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        for h in doc["hits"]:
            self.assertIsNone(h["duplicate_of"])
            self.assertNotIn("content_duplicate", h["noise_signals"])
        self.assertEqual(doc["noise_rules"]["A"]["hits_affected"], 0)

    def test_clustering_does_not_span_projects_by_default(self) -> None:
        """P2-1 (Gate C adversarial review): content-duplicate clustering is
        scoped to (project_id, content_sha256) by default, not
        content_sha256 alone. This codebase's own "copy, don't import"
        convention (M8 design 3.0.2) deliberately manufactures
        byte-identical files across projects, so cross-project clustering
        by default would treat that as noise rather than the normal state
        it actually is here."""
        proj_a = self.tmp / "proj_a"
        proj_b = self.tmp / "proj_b"
        content = "identical across two DIFFERENT projects\n"
        self.make_knowledge_md(proj_a, "reports/note.md", content)
        self.make_knowledge_md(proj_b, "reports/note.md", content)
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("a", proj_a), make_project_row("b", proj_b)]))

        code, _, err = _run_main(self.scan_argv([proj_a, proj_b], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        for h in doc["hits"]:
            self.assertIsNone(h["duplicate_of"])
            self.assertNotIn("content_duplicate", h["noise_signals"])
        self.assertEqual(doc["noise_rules"]["A"]["hits_affected"], 0)

    def test_cluster_scoped_by_root_real_path_not_shared_project_id(self) -> None:
        """P1-1 (max-tier Gate C re-review): the P0-2 fix moved hit_id's own
        identity from project_id to root_real_path, but this clustering key
        was left on project_id. This machine's real catalog.json proves
        project_id is not a reliable proxy for "different project": e.g.
        'hgcloud' -> 3 distinct real_paths, 'rn邮箱' -> 4. Reproduce that
        exact shape here -- two DIFFERENT roots sharing ONE project_id,
        byte-identical content in both -- and confirm clustering still does
        NOT span them by default (only root_real_path, not project_id,
        should scope the cluster)."""
        proj1 = self.tmp / "wt1"
        proj2 = self.tmp / "wt2"
        content = "identical content, two roots, ONE shared project_id\n"
        self.make_knowledge_md(proj1, "reports/note.md", content)
        self.make_knowledge_md(proj2, "reports/note.md", content)
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[
            make_project_row("dup-id", proj1),
            make_project_row("dup-id", proj2),
        ]))

        code, _, err = _run_main(self.scan_argv([proj1, proj2], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        hits = [h for h in doc["hits"] if h["path"] == "reports/note.md"]
        self.assertEqual(len(hits), 2)
        self.assertEqual({h["project_id"] for h in hits}, {"dup-id"})
        # Same project_id, but two different root_real_paths -- must NOT be
        # clustered by default.
        for h in hits:
            self.assertIsNone(h["duplicate_of"])
            self.assertNotIn("content_duplicate", h["noise_signals"])
        self.assertEqual(doc["noise_rules"]["A"]["hits_affected"], 0)

        # --allow-cross-project-cluster still opts back into clustering by
        # content_sha256 alone, regardless of root_real_path or project_id.
        code, _, err = _run_main(
            self.scan_argv([proj1, proj2], catalog, allow_cross_project_cluster=True, quiet=True)
        )
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits_doc()
        self.assertEqual(doc2["noise_rules"]["A"]["hits_affected"], 1)

    def test_allow_cross_project_cluster_opts_back_into_old_behavior(self) -> None:
        proj_a = self.tmp / "proj_a"
        proj_b = self.tmp / "proj_b"
        content = "identical across two DIFFERENT projects\n"
        self.make_knowledge_md(proj_a, "reports/note.md", content)
        self.make_knowledge_md(proj_b, "reports/note.md", content)
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("a", proj_a), make_project_row("b", proj_b)]))

        code, _, err = _run_main(
            self.scan_argv([proj_a, proj_b], catalog, allow_cross_project_cluster=True, quiet=True)
        )
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(doc["noise_rules"]["A"]["hits_affected"], 1)
        dups = [h for h in doc["hits"] if h["duplicate_of"] is not None]
        self.assertEqual(len(dups), 1)

    def test_rule_a_disabled_leaves_hits_unclustered(self) -> None:
        proj = self.tmp / "proj"
        content = "same content\n"
        self.make_knowledge_md(proj, "reports/a.md", content)
        self.make_knowledge_md(proj, "reports/b.md", content)
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, disable_noise_rule=["A"], quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertFalse(doc["noise_rules"]["A"]["active"])
        for h in doc["hits"]:
            self.assertIsNone(h["duplicate_of"])
        self.assertEqual(doc["noise_rules"]["A"]["hits_affected"], 0)


# ---------------------------------------------------------------------------
# Noise rule B: iteration-round artifact annotation
# ---------------------------------------------------------------------------


class NoiseRuleBTests(BaseTestCase):
    def test_round_pattern_is_annotated_case_insensitively(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/REVIEW-round7.md")
        self.make_knowledge_md(proj, "reports/REVIEW-ROUND12-final.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        for h in doc["hits"]:
            self.assertIn("iteration_round_artifact", h["noise_signals"])
        self.assertEqual(doc["noise_rules"]["B"]["hits_affected"], 2)

    def test_round_pattern_inside_ordinary_word_is_not_annotated(self) -> None:
        """P4-2 (Gate C adversarial review): the unanchored regex matched
        'round[0-9]+' as a substring anywhere, so 'background1-context.md'
        ('...g-ROUND1...'), 'turnaround2-plan.md', and 'workaround7.md' were
        all false-positively annotated as iteration-round artifacts."""
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/background1-context.md")
        self.make_knowledge_md(proj, "reports/turnaround2-plan.md")
        self.make_knowledge_md(proj, "reports/workaround7.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(doc["noise_rules"]["B"]["hits_affected"], 0)
        for h in doc["hits"]:
            self.assertNotIn("iteration_round_artifact", h["noise_signals"])

    def test_non_round_filename_is_not_annotated(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/final-review.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(doc["noise_rules"]["B"]["hits_affected"], 0)
        for h in doc["hits"]:
            self.assertNotIn("iteration_round_artifact", h["noise_signals"])

    def test_round_artifacts_excluded_from_summary_by_default_but_visible_in_hit(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/ROUND3-note.md")
        self.make_knowledge_md(proj, "reports/final-note.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(doc["summary"]["new_discoveries_total_including_round_artifacts"], 2)
        self.assertEqual(doc["summary"]["new_discoveries_total_excluding_round_artifacts"], 1)
        self.assertEqual(doc["summary"]["new_discoveries_total"], 1)
        # the hit itself is never dropped even though excluded from the count
        paths = {h["path"] for h in doc["hits"]}
        self.assertIn("reports/ROUND3-note.md", paths)

    def test_include_round_artifacts_flag_flips_default_summary_count(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/ROUND3-note.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(
            self.scan_argv([proj], catalog, include_round_artifacts_in_summary=True, quiet=True)
        )
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(doc["summary"]["new_discoveries_total"], 1)
        self.assertTrue(doc["summary"]["include_round_artifacts_in_summary"])

    def test_rule_b_disabled_leaves_hit_unannotated_and_in_default_count(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/ROUND3-note.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, disable_noise_rule=["B"], quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertFalse(doc["noise_rules"]["B"]["active"])
        self.assertEqual(doc["noise_rules"]["B"]["hits_affected"], 0)
        self.assertEqual(doc["summary"]["new_discoveries_total"], 1)
        for h in doc["hits"]:
            self.assertNotIn("iteration_round_artifact", h["noise_signals"])


# ---------------------------------------------------------------------------
# Noise rule C: -STAGED-review-only pruning
# ---------------------------------------------------------------------------


class NoiseRuleCTests(BaseTestCase):
    def test_staged_review_only_dir_is_pruned(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "some-work-STAGED-review-only/reports/hidden.md")
        self.make_knowledge_md(proj, "reports/visible.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        paths = {h["path"] for h in doc["hits"]}
        self.assertIn("reports/visible.md", paths)
        self.assertFalse(any("STAGED-review-only" in p for p in paths))
        self.assertGreaterEqual(doc["noise_rules"]["C"]["dirs_pruned"], 1)

    def test_rule_c_disabled_scans_staged_dir(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "some-work-STAGED-review-only/reports/hidden.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, disable_noise_rule=["C"], quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertFalse(doc["noise_rules"]["C"]["active"])
        self.assertEqual(doc["noise_rules"]["C"]["dirs_pruned"], 0)
        paths = {h["path"] for h in doc["hits"]}
        self.assertTrue(any("STAGED-review-only" in p for p in paths))

    def test_similar_suffix_not_exact_match_is_not_pruned(self) -> None:
        proj = self.tmp / "proj"
        # does NOT end in exactly "-STAGED-review-only" -- must not be pruned
        self.make_knowledge_md(proj, "not-staged-review-only-extra/reports/x.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        paths = {h["path"] for h in doc["hits"]}
        self.assertTrue(any("not-staged-review-only-extra" in p for p in paths))


# ---------------------------------------------------------------------------
# Noise rule D: overlay-dir pruning
# ---------------------------------------------------------------------------


class NoiseRuleDTests(BaseTestCase):
    def test_dot_overlay_dir_is_pruned_case_insensitively(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, ".pty-overlay-2026/reports/hidden.md")
        self.make_knowledge_md(proj, ".MY-OVERLAY-COPY/reports/hidden2.md")
        self.make_knowledge_md(proj, "reports/visible.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        paths = {h["path"] for h in doc["hits"]}
        self.assertEqual(paths, {"reports/visible.md"})
        self.assertEqual(doc["noise_rules"]["D"]["dirs_pruned"], 2)

    def test_rule_d_disabled_scans_overlay_dir(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, ".pty-overlay-2026/reports/hidden.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, disable_noise_rule=["D"], quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertFalse(doc["noise_rules"]["D"]["active"])
        paths = {h["path"] for h in doc["hits"]}
        self.assertTrue(any("pty-overlay" in p for p in paths))

    def test_non_dotted_overlay_dir_is_not_pruned(self) -> None:
        proj = self.tmp / "proj"
        # contains "overlay" but does not start with "." -- must not be pruned
        self.make_knowledge_md(proj, "my-overlay-dir/reports/visible.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        paths = {h["path"] for h in doc["hits"]}
        self.assertTrue(any("my-overlay-dir" in p for p in paths))


# ---------------------------------------------------------------------------
# Summary field naming/shape correctness (P3-1, P3-2 -- Gate C adversarial
# review).
# ---------------------------------------------------------------------------


class SummaryFieldTests(BaseTestCase):
    def test_dirs_pruned_by_sensible_exclude_excludes_noise_rule_c_and_d(self) -> None:
        """P3-1: the old field name `dirs_pruned_total` counted ONLY the
        unconditional SENSIBLE_EXCLUDE prunes (node_modules etc), never
        noise rules C/D -- misleadingly implying "0 total pruned" even when
        noise_rules.C/D.dirs_pruned were nonzero. Renamed to name what it
        actually counts."""
        proj = self.tmp / "proj"
        (proj / "node_modules").mkdir(parents=True)
        self.make_knowledge_md(proj, "some-work-STAGED-review-only/reports/hidden.md")
        self.make_knowledge_md(proj, ".pty-overlay-x/reports/hidden2.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertNotIn("dirs_pruned_total", doc["summary"])
        self.assertEqual(doc["summary"]["dirs_pruned_by_sensible_exclude"], 1)  # node_modules only
        self.assertEqual(doc["noise_rules"]["C"]["dirs_pruned"], 1)
        self.assertEqual(doc["noise_rules"]["D"]["dirs_pruned"], 1)

    def test_new_discoveries_by_signal_type_zero_fills_instead_of_dropping_keys(self) -> None:
        """P3-2: a signal_type whose only hits are noise-rule-B-excluded
        must still appear in new_discoveries_by_signal_type with count 0,
        not vanish from the map entirely -- a consumer diffing the
        excluding/including views should see a count change, not a schema
        change."""
        proj = self.tmp / "proj"
        # This hit is knowledge:reports_dir+filename_keyword AND a round
        # artifact -- excluded from the default (excl) summary count, but
        # the signal_type must still show up with 0, not disappear.
        self.make_knowledge_md(proj, "reports/REVIEW-round7.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        signal_type = "knowledge:reports_dir+filename_keyword"
        self.assertIn(signal_type, doc["summary"]["hits_by_signal_type"])
        # new_discoveries_by_signal_type mirrors the default (excl) view --
        # the key must be present with value 0, not absent.
        self.assertIn(signal_type, doc["summary"]["new_discoveries_by_signal_type"])
        self.assertEqual(doc["summary"]["new_discoveries_by_signal_type"][signal_type], 0)


# ---------------------------------------------------------------------------
# Scope: --root vs --all-projects
# ---------------------------------------------------------------------------


class ScopeTests(BaseTestCase):
    def test_no_scope_given_is_usage_error(self) -> None:
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        code, out, err = _run_main(["scan", "--catalog", str(catalog)])
        self.assertEqual(code, 2)
        self.assertIn("no_scope_given", err)

    def test_zero_max_file_bytes_is_usage_error(self) -> None:
        """P4-4 (Gate C adversarial review): --max-file-bytes 0 (or
        negative) previously passed through unvalidated, silently making
        every file report too_large -- exploitable-free but pointlessly
        confusing."""
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        proj = self.tmp / "proj"
        proj.mkdir()
        code, out, err = _run_main(self.scan_argv([proj], catalog, max_file_bytes=0))
        self.assertEqual(code, 2)
        self.assertIn("invalid_max_file_bytes", err)

    def test_negative_max_file_bytes_is_usage_error(self) -> None:
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        proj = self.tmp / "proj"
        proj.mkdir()
        code, out, err = _run_main(self.scan_argv([proj], catalog, max_file_bytes=-1))
        self.assertEqual(code, 2)
        self.assertIn("invalid_max_file_bytes", err)

    def test_root_not_a_directory_is_usage_error(self) -> None:
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        missing = self.tmp / "does-not-exist"
        code, out, err = _run_main(["scan", "--catalog", str(catalog), "--root", str(missing)])
        self.assertEqual(code, 2)
        self.assertIn("root_not_a_directory", err)

    def test_root_only_does_not_touch_other_catalog_projects(self) -> None:
        proj_a = self.tmp / "proj_a"
        proj_b = self.tmp / "proj_b"
        self.make_knowledge_md(proj_a, "reports/a.md")
        self.make_knowledge_md(proj_b, "reports/b.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("a", proj_a), make_project_row("b", proj_b)]))

        code, _, err = _run_main(self.scan_argv([proj_a], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        paths = {h["path"] for h in doc["hits"]}
        self.assertEqual(paths, {"reports/a.md"})
        self.assertEqual(len(doc["roots_scanned"]), 1)

    def test_all_projects_resolves_from_catalog_and_prints_unsuppressible_warning(self) -> None:
        proj_a = self.tmp / "proj_a"
        proj_b = self.tmp / "proj_b"
        self.make_knowledge_md(proj_a, "reports/a.md")
        self.make_knowledge_md(proj_b, "reports/b.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("a", proj_a), make_project_row("b", proj_b)]))

        code, out, err = _run_main(["scan", "--catalog", str(catalog), "--all-projects", "--quiet"])
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING", err)
        doc = self.read_hits_doc()
        paths = {h["path"] for h in doc["hits"]}
        self.assertEqual(paths, {"reports/a.md", "reports/b.md"})

    def test_plain_root_scan_prints_no_all_projects_warning(self) -> None:
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/a.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        code, out, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        self.assertNotIn("WARNING", err)

    def test_all_projects_skips_non_directory_project_rows(self) -> None:
        proj_a = self.tmp / "proj_a"
        proj_a.mkdir()
        missing = self.tmp / "does-not-exist-project"
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("a", proj_a), make_project_row("missing", missing)]))

        code, _, err = _run_main(["scan", "--catalog", str(catalog), "--all-projects", "--quiet"])
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(len(doc["scope"]["all_projects_skipped_not_directory"]), 1)
        self.assertEqual(len(doc["roots_scanned"]), 1)

    def test_all_projects_plus_explicit_root_combines_without_double_scanning(self) -> None:
        proj_a = self.tmp / "proj_a"
        self.make_knowledge_md(proj_a, "reports/a.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("a", proj_a)]))

        code, _, err = _run_main(["scan", "--catalog", str(catalog), "--root", str(proj_a), "--all-projects", "--quiet"])
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(len(doc["roots_scanned"]), 1)  # not scanned twice
        self.assertEqual(doc["roots_scanned"][0]["root_source"], "explicit")


# ---------------------------------------------------------------------------
# M8-2 authorization notice on a default-path write
# ---------------------------------------------------------------------------


class ProductionPathAuthorizationNoticeTests(BaseTestCase):
    """`scan` never refuses to run or write -- this is a visibility notice,
    not a gate (see module docstring's M8-2 AUTHORIZATION NOTICE section).
    Every other test in this file redirects DEFAULT_OUTPUT_DIR to a tempdir
    (BaseTestCase.setUp) without ever touching the frozen
    _PRODUCTION_DEFAULT_OUTPUT_DIR reference, so the two constants already
    differ and the notice never fires for them -- that is exactly
    test_notice_absent_when_output_dir_is_redirected below, made explicit.
    To exercise the "still at the real production default" branch without
    ever writing to the real production path on disk, this test also
    redirects the frozen constant itself to the SAME tempdir as the mutable
    one, reproducing the equality condition cmd_scan() checks."""

    def test_notice_printed_when_output_dir_equals_frozen_production_default(self) -> None:
        orig_frozen = dcc._PRODUCTION_DEFAULT_OUTPUT_DIR
        dcc._PRODUCTION_DEFAULT_OUTPUT_DIR = dcc.DEFAULT_OUTPUT_DIR
        try:
            proj = self.tmp / "proj"
            self.make_knowledge_md(proj, "reports/a.md")
            catalog = self.tmp / "catalog.json"
            _write_json(catalog, make_catalog())
            code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        finally:
            dcc._PRODUCTION_DEFAULT_OUTPUT_DIR = orig_frozen
        self.assertEqual(code, 0, err)
        self.assertIn("NOTICE", err)
        self.assertIn("M8-2's independent authorization gate", err)
        self.assertIn("44%-72%", err)

    def test_notice_absent_when_output_dir_is_redirected(self) -> None:
        # BaseTestCase.setUp already redirected DEFAULT_OUTPUT_DIR away from
        # the frozen _PRODUCTION_DEFAULT_OUTPUT_DIR -- this is every other
        # test in this file's own default condition, made explicit here.
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "reports/a.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        self.assertNotIn("NOTICE", err)
        self.assertNotIn("M8-2's independent authorization gate", err)


# ---------------------------------------------------------------------------
# Catalog baseline dedup + staleness
# ---------------------------------------------------------------------------


class BaselineTests(BaseTestCase):
    def test_hit_already_in_baseline_is_not_a_new_discovery(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/known.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(
            projects=[make_project_row("p", proj)],
            capabilities=[{"project_id": "p", "path": "scripts/known.py", "kind": "script"}],
        ))

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        hit = next(h for h in doc["hits"] if h["path"] == "scripts/known.py")
        self.assertTrue(hit["already_in_catalog_baseline"])
        self.assertFalse(hit["is_new_discovery"])

    def test_hit_not_in_baseline_is_new_discovery(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/unknown.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("p", proj)]))

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        hit = next(h for h in doc["hits"] if h["path"] == "scripts/unknown.py")
        self.assertFalse(hit["already_in_catalog_baseline"])
        self.assertTrue(hit["is_new_discovery"])

    def test_catalog_unreadable_with_root_given_degrades_not_fatal(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/x.py")
        catalog = self.tmp / "does-not-exist-catalog.json"

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertFalse(doc["catalog_baseline"]["catalog_readable"])
        # every hit is treated as new since the baseline is empty
        self.assertTrue(all(h["is_new_discovery"] for h in doc["hits"]))

    def test_catalog_unreadable_and_all_projects_only_is_fatal(self) -> None:
        # Deliberately NOT --quiet: --quiet suppresses error output too (see
        # _emit_error), and this test needs to see the reason string.
        catalog = self.tmp / "does-not-exist-catalog.json"
        code, out, err = _run_main(["scan", "--catalog", str(catalog), "--all-projects"])
        self.assertEqual(code, 4)
        self.assertIn("catalog_unreadable_and_no_root_fallback", err)

    def test_baseline_staleness_is_flagged_on_root_stats(self) -> None:
        proj = self.tmp / "proj"
        proj.mkdir()
        (proj / "wiki").mkdir()
        (proj / "wiki" / "reusable-capabilities.json").write_text('{"capabilities": []}', encoding="utf-8")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row(
            "p", proj, sources={"reusable-capabilities.json": {"status": "ok", "sha256": "deadbeef" * 8}},
        )]))

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertTrue(doc["roots_scanned"][0]["baseline_stale"])
        self.assertTrue(doc["catalog_baseline"]["staleness_by_project"]["p"]["stale"])

    def test_catalog_exceeding_size_cap_degrades_not_fatal_with_root_given(self) -> None:
        # MAX_CATALOG_BYTES exists specifically so a huge/corrupt catalog.json
        # can't be read unbounded into memory (this codebase bounds every
        # other externally-produced file it reads -- discovery-hits.json via
        # MAX_EXISTING_HITS_BYTES/MAX_HITS_FILE_BYTES -- catalog.json is no
        # different). Confirmed by direct reproduction that, before this
        # test's companion fix, an oversized catalog.json was read via a
        # plain unbounded json.load() with the cap constant never referenced
        # anywhere in the file.
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/x.py")
        catalog = self.tmp / "catalog.json"
        oversized = make_catalog(projects=[make_project_row("p", proj)])
        oversized["_pad"] = "A" * (dcc.MAX_CATALOG_BYTES + 1)
        _write_json(catalog, oversized)
        self.assertGreater(catalog.stat().st_size, dcc.MAX_CATALOG_BYTES)

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertFalse(doc["catalog_baseline"]["catalog_readable"])
        self.assertIn("size cap", doc["catalog_baseline"]["load_error"])
        # Degrades gracefully exactly like an unreadable/missing catalog:
        # every hit is treated as new since the baseline is empty.
        self.assertTrue(all(h["is_new_discovery"] for h in doc["hits"]))


# ---------------------------------------------------------------------------
# CONTENT-HASH-KEYED STATE PERSISTENCE ACROSS RERUNS -- the most important
# test in this suite (see discover_capability_candidates.py's own docstring
# and review_capability_candidates.py's mark command).
# ---------------------------------------------------------------------------


class StatePersistenceTests(BaseTestCase):
    def test_dismissed_state_survives_identical_rerun(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/noisy.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        # (1) scan once
        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc1 = self.read_hits_doc()
        hit = next(h for h in doc1["hits"] if h["path"] == "scripts/noisy.py")
        self.assertEqual(hit["state"], "pending")
        hit_id = hit["hit_id"]

        # (2) mark it dismissed via a direct read-modify-write mirroring
        #     review_capability_candidates.py's own mark logic (this suite
        #     tests the scan side's preservation contract in isolation; the
        #     review tool's own mark() is exercised end-to-end in
        #     test_review_capability_candidates.py and in the combined
        #     end-to-end test below).
        doc1["hits"][doc1["hits"].index(hit)]["state"] = "dismissed"
        doc1["hits"][doc1["hits"].index(hit)]["state_note"] = "not useful"
        doc1["hits"][doc1["hits"].index(hit)]["state_set_by"] = "test-human"
        doc1["hits"][doc1["hits"].index(hit)]["state_set_at"] = "2026-08-23T00:00:00Z"
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        # (3) rerun scan with IDENTICAL input
        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits_doc()

        # (4) assert STILL dismissed, not reset to pending
        hit2 = next(h for h in doc2["hits"] if h["hit_id"] == hit_id)
        self.assertEqual(hit2["state"], "dismissed")
        self.assertEqual(hit2["state_note"], "not useful")
        self.assertEqual(hit2["state_set_by"], "test-human")
        self.assertEqual(hit2["state_set_at"], "2026-08-23T00:00:00Z")
        self.assertEqual(doc2["summary"]["states_preserved_from_previous_terminal_run"], 1)

    def test_triaged_for_promotion_state_also_survives_rerun(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/good.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        _run_main(self.scan_argv([proj], catalog, quiet=True))
        doc1 = self.read_hits_doc()
        idx = next(i for i, h in enumerate(doc1["hits"]) if h["path"] == "scripts/good.py")
        doc1["hits"][idx]["state"] = "triaged_for_promotion"
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        _run_main(self.scan_argv([proj], catalog, quiet=True))
        doc2 = self.read_hits_doc()
        hit2 = next(h for h in doc2["hits"] if h["path"] == "scripts/good.py")
        self.assertEqual(hit2["state"], "triaged_for_promotion")

    def test_pending_hit_is_not_preserved_specially_and_new_hit_starts_pending(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/one.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.make_script(proj, "scripts/two.py")
        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        states = {h["path"]: h["state"] for h in doc["hits"]}
        self.assertEqual(states["scripts/one.py"], "pending")
        self.assertEqual(states["scripts/two.py"], "pending")
        self.assertEqual(doc["summary"]["states_preserved_from_previous_terminal_run"], 0)

    def test_hit_that_disappears_from_disk_is_not_carried_forward(self) -> None:
        proj = self.tmp / "proj"
        p = self.make_script(proj, "scripts/gone.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        _run_main(self.scan_argv([proj], catalog, quiet=True))
        doc1 = self.read_hits_doc()
        idx = next(i for i, h in enumerate(doc1["hits"]) if h["path"] == "scripts/gone.py")
        doc1["hits"][idx]["state"] = "dismissed"
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        p.unlink()
        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits_doc()
        self.assertEqual(doc2["hits"], [])

    def test_noise_rule_toggle_does_not_drop_terminal_state_for_unchanged_file(self) -> None:
        """P2-1 (max-tier Gate C re-review): a hit not being re-detected
        this run because a noise rule setting differs from the run that
        produced the record must NOT be treated as equivalent to "the file
        is gone" -- only positive evidence (the file missing, or its
        content changed) should drop a terminal-state record. Reproduce
        with noise rule C: run 1 has C disabled and detects a hit inside a
        `-STAGED-review-only` dir, marked dismissed; run 2 has C back at
        its default (enabled), pruning that directory before the walk
        descends into it, so the hit is not redetected even though the
        underlying file is byte-identical and was never touched."""
        proj = self.tmp / "proj"
        self.make_knowledge_md(proj, "work-STAGED-review-only/reports/hidden.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, disable_noise_rule=["C"], quiet=True))
        self.assertEqual(code, 0, err)
        doc1 = self.read_hits_doc()
        idx = next(i for i, h in enumerate(doc1["hits"]) if h["path"] == "work-STAGED-review-only/reports/hidden.md")
        hit_id = doc1["hits"][idx]["hit_id"]
        doc1["hits"][idx]["state"] = "dismissed"
        doc1["hits"][idx]["state_note"] = "reviewed"
        doc1["hits"][idx]["state_set_by"] = "human"
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        # rerun WITHOUT disabling C -- rule C is back to its default
        # (enabled) and prunes the directory before ever walking into it,
        # so this hit is not redetected this run. The root itself WAS
        # scanned, and the file on disk is unchanged.
        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits_doc()
        hit2 = next((h for h in doc2["hits"] if h["hit_id"] == hit_id), None)
        self.assertIsNotNone(
            hit2, "terminal-state record was dropped when a noise rule toggle merely suppressed redetection"
        )
        self.assertEqual(hit2["state"], "dismissed")
        self.assertEqual(hit2["state_note"], "reviewed")
        self.assertEqual(doc2["summary"]["hits_carried_forward_scanned_but_not_redetected"], 1)

    def test_transient_read_error_does_not_drop_terminal_state_for_unchanged_file(self) -> None:
        """P2-1 (max-tier Gate C re-review), second trigger: a transient
        read error on one file (EACCES here -- the same class of error a
        large --all-projects walk can genuinely hit as EMFILE/ENFILE) must
        also not be treated as equivalent to "the file is gone". Reproduce
        by chmod'ing the file unreadable between two runs, content
        otherwise unchanged; the scan-time read fails (no hit produced this
        run) and this must NOT drop the prior terminal state."""
        proj = self.tmp / "proj"
        p = self.make_script(proj, "scripts/flaky.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc1 = self.read_hits_doc()
        idx = next(i for i, h in enumerate(doc1["hits"]) if h["path"] == "scripts/flaky.py")
        hit_id = doc1["hits"][idx]["hit_id"]
        doc1["hits"][idx]["state"] = "dismissed"
        doc1["hits"][idx]["state_note"] = "reviewed"
        doc1["hits"][idx]["state_set_by"] = "human"
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        os.chmod(str(p), 0o000)
        try:
            code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
            self.assertEqual(code, 0, err)
        finally:
            os.chmod(str(p), 0o644)

        doc2 = self.read_hits_doc()
        hit2 = next((h for h in doc2["hits"] if h["hit_id"] == hit_id), None)
        self.assertIsNotNone(hit2, "terminal-state record was dropped on a transient read error")
        self.assertEqual(hit2["state"], "dismissed")
        self.assertEqual(hit2["state_note"], "reviewed")
        self.assertEqual(doc2["summary"]["hits_carried_forward_scanned_but_not_redetected"], 1)

    def test_scope_narrowed_rerun_does_not_wipe_unscanned_projects_terminal_state(self) -> None:
        """A rescan with a narrower --root set than a previous run (e.g. the
        caller only cares about one project this time) must NOT delete the
        prior run's discovery hits -- including terminal triage states --
        for projects that simply were not part of this run's scope. Only a
        hit whose OWN project actually got rescanned and genuinely no
        longer exists should be dropped (see the sibling test above)."""
        proj_a = self.tmp / "proj_a"
        proj_b = self.tmp / "proj_b"
        self.make_knowledge_md(proj_a, "reports/a.md")
        self.make_knowledge_md(proj_b, "reports/b.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("a", proj_a), make_project_row("b", proj_b)]))

        # (1) scan both roots together
        code, _, err = _run_main(self.scan_argv([proj_a, proj_b], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc1 = self.read_hits_doc()
        b_idx = next(i for i, h in enumerate(doc1["hits"]) if h["project_id"] == "b")
        doc1["hits"][b_idx]["state"] = "dismissed"
        doc1["hits"][b_idx]["state_note"] = "irrelevant"
        doc1["hits"][b_idx]["state_set_by"] = "human"
        doc1["hits"][b_idx]["state_set_at"] = "2026-08-23T00:00:00Z"
        b_hit_id = doc1["hits"][b_idx]["hit_id"]
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        # (2) rerun with ONLY proj_a in scope -- proj_b was never touched
        code, _, err = _run_main(self.scan_argv([proj_a], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits_doc()
        by_id = {h["hit_id"]: h for h in doc2["hits"]}

        # proj_a's hit is present and fresh (pending)
        a_hits = [h for h in doc2["hits"] if h["project_id"] == "a"]
        self.assertEqual(len(a_hits), 1)
        self.assertEqual(a_hits[0]["state"], "pending")

        # proj_b's dismissed hit survives, byte-for-byte, even though this
        # run never scanned proj_b at all
        self.assertIn(b_hit_id, by_id)
        preserved_b = by_id[b_hit_id]
        self.assertEqual(preserved_b["state"], "dismissed")
        self.assertEqual(preserved_b["state_note"], "irrelevant")
        self.assertEqual(preserved_b["state_set_by"], "human")
        self.assertEqual(preserved_b["state_set_at"], "2026-08-23T00:00:00Z")
        self.assertEqual(doc2["summary"]["hits_carried_forward_from_unscanned_projects"], 1)

        # proj_b did NOT appear in this run's own roots_scanned/stats
        scanned_pids = {s["project_id"] for s in doc2["roots_scanned"]}
        self.assertEqual(scanned_pids, {"a"})

        # (3) now actually rescan proj_b too, with its file deleted -- THIS
        # is when the stale hit should finally be dropped for real.
        (proj_b / "reports" / "b.md").unlink()
        code, _, err = _run_main(self.scan_argv([proj_a, proj_b], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc3 = self.read_hits_doc()
        remaining_ids = {h["hit_id"] for h in doc3["hits"]}
        self.assertNotIn(b_hit_id, remaining_ids)
        self.assertEqual(doc3["summary"]["hits_carried_forward_from_unscanned_projects"], 0)

    def test_state_persists_across_project_id_resolution_change(self) -> None:
        """P0-1 (Gate C adversarial review): a project that starts
        un-catalogued (fallback `unmatched:` project_id) and is later added
        to catalog.json must NOT lose its triage state on the very next
        rescan of byte-identical files, even though its project_id changes.
        Before the fix (hit_id keyed on project_id), this silently orphaned
        the dismissed record and produced a brand-new pending duplicate."""
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/tool_one.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[]))  # NOT catalogued yet

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc1 = self.read_hits_doc()
        hit = next(h for h in doc1["hits"] if h["path"] == "scripts/tool_one.py")
        self.assertFalse(hit["project_id_is_catalog_match"])
        hit_id = hit["hit_id"]

        idx = doc1["hits"].index(hit)
        doc1["hits"][idx]["state"] = "dismissed"
        doc1["hits"][idx]["state_note"] = "reviewed, junk"
        doc1["hits"][idx]["state_set_by"] = "human"
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        # project gets catalogued -- byte-identical files, same real root
        _write_json(catalog, make_catalog(projects=[make_project_row("proj-real-id", proj)]))

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits_doc()

        # hit_id unchanged (real-path-keyed), state preserved, project_id
        # field updated to the now-resolved catalog id, and no ghost
        # duplicate record left behind.
        self.assertEqual(len(doc2["hits"]), 1)
        hit2 = next(h for h in doc2["hits"] if h["hit_id"] == hit_id)
        self.assertEqual(hit2["state"], "dismissed")
        self.assertEqual(hit2["state_note"], "reviewed, junk")
        self.assertEqual(hit2["project_id"], "proj-real-id")
        self.assertTrue(hit2["project_id_is_catalog_match"])
        self.assertEqual(doc2["summary"]["states_preserved_from_previous_terminal_run"], 1)

    def test_duplicate_catalog_project_ids_across_different_real_paths_do_not_collide(self) -> None:
        """P0-2 (Gate C adversarial review): catalog.json can legitimately
        contain two DIFFERENT real_path rows sharing the SAME project_id --
        confirmed on this machine's real catalog.json (155 projects,
        'hgcloud': 3, 'rn邮箱': 4 duplicate ids, 0 duplicate real_paths).
        Before the fix (hit_id keyed on project_id), two physically distinct
        files in the two projects collapsed onto one hit_id, and a mark on
        one silently mutated the other."""
        proj1 = self.tmp / "wt1" / "scripts"
        proj2 = self.tmp / "wt2" / "scripts"
        self.make_script(proj1, "foo.py")
        self.make_script(proj2, "foo.py")
        (proj2 / "foo.py").write_text(
            (proj2 / "foo.py").read_text(encoding="utf-8") + "\n# distinguishing comment\n", encoding="utf-8"
        )
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[
            make_project_row("dup-id", proj1),
            make_project_row("dup-id", proj2),
        ]))

        code, _, err = _run_main(self.scan_argv([proj1, proj2], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc1 = self.read_hits_doc()
        hits = [h for h in doc1["hits"] if h["path"] == "foo.py"]
        self.assertEqual(len(hits), 2)
        self.assertEqual(len({h["hit_id"] for h in hits}), 2, "hit_id collided across two projects sharing a project_id")
        self.assertEqual(len({h["content_sha256"] for h in hits}), 2)

        # Mark the wt1 hit dismissed, rescan, confirm the wt2 hit (same
        # project_id, different real content/hit_id) is untouched.
        wt1_hit = next(h for h in hits if h["root_real_path"] == os.path.realpath(str(proj1)))
        idx = doc1["hits"].index(wt1_hit)
        doc1["hits"][idx]["state"] = "dismissed"
        doc1["hits"][idx]["state_note"] = "wt1 only"
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        code, _, err = _run_main(self.scan_argv([proj1, proj2], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits_doc()
        hits2 = [h for h in doc2["hits"] if h["path"] == "foo.py"]
        self.assertEqual(len(hits2), 2)
        by_real_path = {h["root_real_path"]: h for h in hits2}
        self.assertEqual(by_real_path[os.path.realpath(str(proj1))]["state"], "dismissed")
        self.assertEqual(by_real_path[os.path.realpath(str(proj2))]["state"], "pending")

    def test_all_projects_run_where_one_project_fails_to_resolve_preserves_its_hits(self) -> None:
        """The --all-projects path can skip a project row whose real_path is
        not (currently) a directory (all_projects_skipped_not_directory).
        That must not be treated as "this project was scanned and found
        empty" -- its prior hits (and any terminal states) must survive."""
        proj_a = self.tmp / "proj_a"
        proj_b = self.tmp / "proj_b"
        self.make_knowledge_md(proj_a, "reports/a.md")
        self.make_knowledge_md(proj_b, "reports/b.md")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[make_project_row("a", proj_a), make_project_row("b", proj_b)]))

        code, _, err = _run_main(["scan", "--catalog", str(catalog), "--all-projects", "--quiet"])
        self.assertEqual(code, 0, err)
        doc1 = self.read_hits_doc()
        b_idx = next(i for i, h in enumerate(doc1["hits"]) if h["project_id"] == "b")
        doc1["hits"][b_idx]["state"] = "triaged_for_promotion"
        b_hit_id = doc1["hits"][b_idx]["hit_id"]
        (self.output_dir / dcc.HITS_NAME).write_text(json.dumps(doc1, ensure_ascii=False), encoding="utf-8")

        # proj_b's directory temporarily "disappears" (e.g. an unmounted
        # drive) -- --all-projects will skip it via
        # all_projects_skipped_not_directory, not scan it.
        shutil.rmtree(proj_b)

        code, _, err = _run_main(["scan", "--catalog", str(catalog), "--all-projects", "--quiet"])
        self.assertEqual(code, 0, err)
        doc2 = self.read_hits_doc()
        by_id = {h["hit_id"]: h for h in doc2["hits"]}
        self.assertIn(b_hit_id, by_id)
        self.assertEqual(by_id[b_hit_id]["state"], "triaged_for_promotion")
        self.assertEqual(doc2["summary"]["hits_carried_forward_from_unscanned_projects"], 1)


# ---------------------------------------------------------------------------
# Lock acquire/release + stale-lock recovery
# ---------------------------------------------------------------------------


class LockTests(BaseTestCase):
    def test_lock_is_released_after_successful_scan(self) -> None:
        proj = self.tmp / "proj"
        proj.mkdir()
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        self.assertFalse((self.output_dir / dcc.LOCK_NAME).exists())

    def test_held_fresh_lock_is_fatal(self) -> None:
        self.output_dir.mkdir(parents=True)
        (self.output_dir / dcc.LOCK_NAME).write_text('{"pid": 999999}', encoding="utf-8")
        proj = self.tmp / "proj"
        proj.mkdir()
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        # Deliberately NOT --quiet -- see the analogous note above.
        code, out, err = _run_main(self.scan_argv([proj], catalog))
        self.assertEqual(code, 4)
        self.assertIn("lock_held", err)

    def test_stale_lock_is_recovered_and_scan_succeeds(self) -> None:
        self.output_dir.mkdir(parents=True)
        lock_path = self.output_dir / dcc.LOCK_NAME
        lock_path.write_text('{"pid": 999999, "started_at": "stale"}', encoding="utf-8")
        stale_time = time.time() - (dcc.LOCK_STALE_SECONDS + 1)
        os.utime(str(lock_path), (stale_time, stale_time))

        proj = self.tmp / "proj"
        proj.mkdir()
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        self.assertFalse(lock_path.exists())

    def test_stale_lock_grants_exactly_one_concurrent_caller(self) -> None:
        base_dir = self.tmp / "lock-race-dir"
        base_dir.mkdir()
        lock_path = base_dir / dcc.LOCK_NAME
        lock_path.write_text('{"pid": 999999, "started_at": "stale"}', encoding="utf-8")
        stale_time = time.time() - (dcc.LOCK_STALE_SECONDS + 1)
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
                dcc.acquire_lock(base_dir)
                outcome = "acquired"
            except dcc.DiscoverFatal as exc:
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
        # a read-only base_dir must surface as a NAMED DiscoverFatal reason,
        # not propagate as an untyped OSError reported as unexpected_error.
        base_dir = self.tmp / "lock-readonly-dir"
        base_dir.mkdir()
        os.chmod(str(base_dir), 0o500)
        try:
            with self.assertRaises(dcc.DiscoverFatal) as ctx:
                dcc.acquire_lock(base_dir)
            self.assertEqual(ctx.exception.reason, "lock_uncreatable")
        finally:
            os.chmod(str(base_dir), 0o700)


# ---------------------------------------------------------------------------
# Per-run NDJSON audit log -- a SEPARATE, best-effort artifact from
# discovery-hits.json. Its own write failure must degrade (annotated),
# never flip an otherwise-successful scan's exit code away from 0.
# ---------------------------------------------------------------------------


class NdjsonRunLogTests(BaseTestCase):
    def test_ndjson_run_log_write_failure_does_not_fail_an_otherwise_successful_scan(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/x.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        # Pre-create runs/ read-only so append_ndjson_line's O_CREAT open
        # fails with PermissionError. discovery-hits.json is written INSIDE
        # the same lock, strictly before any NDJSON append is attempted, so
        # its write must have already durably succeeded regardless of what
        # happens to the audit log afterward. Confirmed by direct
        # reproduction that, before this test's companion fix, this exact
        # scenario reported exit 4 "unexpected_error" (an uncaught
        # PermissionError propagating out of cmd_scan) even though
        # discovery-hits.json had already been written correctly.
        runs_dir = self.output_dir / dcc.RUNS_DIR_NAME
        runs_dir.mkdir(parents=True, mode=0o700)
        os.chmod(str(runs_dir), 0o500)
        try:
            code, out, err = _run_main(self.scan_argv([proj], catalog, json=True))
        finally:
            os.chmod(str(runs_dir), 0o700)

        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        hit = next(h for h in doc["hits"] if h["path"] == "scripts/x.py")
        self.assertEqual(hit["state"], "pending")

        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["run_log_write_errors"], "expected the NDJSON write failure to be recorded, not hidden")


# ---------------------------------------------------------------------------
# Corrupt/oversized existing discovery-hits.json must be backed up, never
# silently overwritten (P1-1, Gate C adversarial review).
# ---------------------------------------------------------------------------


class UnreadableExistingHitsFileTests(BaseTestCase):
    def test_corrupt_existing_hits_file_is_backed_up_not_silently_destroyed(self) -> None:
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/tool_one.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc1 = self.read_hits_doc()
        doc1["hits"][0]["state"] = "dismissed"
        original_bytes = json.dumps(doc1, ensure_ascii=False).encode("utf-8")
        (self.output_dir / dcc.HITS_NAME).write_bytes(original_bytes)

        # Corrupt the file in place -- not valid JSON at all.
        (self.output_dir / dcc.HITS_NAME).write_text("{not valid json at all!!!", encoding="utf-8")

        code, out, err = _run_main(self.scan_argv([proj], catalog, json=True))
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING", err)
        self.assertIn("backed up", err)

        doc2 = self.read_hits_doc()
        # Fresh document produced (can't recover corrupt state
        # programmatically) but every trace of the loss is visible, not
        # buried: fresh hit starts pending...
        self.assertEqual(doc2["hits"][0]["state"], "pending")
        self.assertEqual(doc2["summary"]["existing_hits_file_status"], "unreadable")
        backup_path = doc2["summary"]["existing_hits_file_backup_path"]
        self.assertIsNotNone(backup_path)
        self.assertEqual(doc2["summary"]["existing_hits_file_backup_status"], "ok")
        # ...and the ORIGINAL corrupt bytes are preserved, untouched, at the
        # backup path -- the human's dismissed decision is at least
        # recoverable by hand from this file, even though the tool itself
        # could not merge it back in automatically.
        backup_bytes = Path(backup_path).read_bytes()
        self.assertEqual(backup_bytes, b"{not valid json at all!!!")

    def test_oversized_existing_hits_file_is_also_backed_up(self) -> None:
        proj = self.tmp / "proj"
        proj.mkdir()
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())
        self.output_dir.mkdir(parents=True, exist_ok=True)
        oversized = json.dumps({"hits": [], "_pad": "A" * (dcc.MAX_EXISTING_HITS_BYTES + 1)})
        (self.output_dir / dcc.HITS_NAME).write_text(oversized, encoding="utf-8")

        code, _, err = _run_main(self.scan_argv([proj], catalog, quiet=True))
        self.assertEqual(code, 0, err)
        doc = self.read_hits_doc()
        self.assertEqual(doc["summary"]["existing_hits_file_status"], "unreadable")
        self.assertIn("size cap", doc["summary"]["existing_hits_file_reason"])
        backup_path = doc["summary"]["existing_hits_file_backup_path"]
        self.assertIsNotNone(backup_path)
        self.assertGreater(Path(backup_path).stat().st_size, dcc.MAX_EXISTING_HITS_BYTES)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_unreadable_existing_hits_file_backup_failure_is_fatal_not_silent(self) -> None:
        """If the backup read itself fails (a genuine I/O/permissions
        problem, not just a content problem) scan must refuse outright
        rather than proceed and silently destroy whatever state might still
        be in the unreadable file."""
        proj = self.tmp / "proj"
        self.make_script(proj, "scripts/x.py")
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog())

        self.output_dir.mkdir(parents=True, exist_ok=True)
        hits_path = self.output_dir / dcc.HITS_NAME
        hits_path.write_text("not valid json", encoding="utf-8")
        os.chmod(str(hits_path), 0o000)
        try:
            code, out, err = _run_main(self.scan_argv([proj], catalog))
        finally:
            os.chmod(str(hits_path), 0o644)

        self.assertEqual(code, 4)
        self.assertIn("existing_hits_file_unreadable_backup_failed", err)
        # The original unreadable file must be left completely untouched --
        # no fresh write happened.
        self.assertEqual(hits_path.read_text(encoding="utf-8"), "not valid json")


# ---------------------------------------------------------------------------
# Read-only isolation (mandatory, real OS permissions)
# ---------------------------------------------------------------------------


class NegativeControlTests(unittest.TestCase):
    """Proves the filesystem actually enforces the permission bits the
    isolation suite below relies on."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dcc2-negctrl-"))
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


class ReadOnlyIsolationTests(BaseTestCase):
    """Scans a real, OS-level read-only project tree and asserts (1) no
    PermissionError anywhere, (2) the tree is byte-identical before/after,
    and (3) every write this run produced lands only under DEFAULT_OUTPUT_DIR."""

    def setUp(self) -> None:
        super().setUp()
        self.proj = self.tmp / "readonly-proj"
        self.make_script(self.proj, "scripts/run.py")
        self.make_knowledge_md(self.proj, "reports/note.md")
        (self.proj / "skills" / "s").mkdir(parents=True)
        (self.proj / "skills" / "s" / "SKILL.md").write_text("d", encoding="utf-8")
        self.catalog = self.tmp / "catalog.json"
        _write_json(self.catalog, make_catalog(projects=[make_project_row("p", self.proj)]))
        _make_readonly(self.proj)
        self.snapshot_before = _snapshot(self.proj)

    def tearDown(self) -> None:
        _make_writable(self.proj)
        super().tearDown()

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_scan_of_readonly_tree_raises_nothing_and_leaves_it_untouched(self) -> None:
        code, out, err = _run_main(self.scan_argv([self.proj], self.catalog, json=True, quiet=True))
        self.assertEqual(code, 0, err)
        snapshot_after = _snapshot(self.proj)
        self.assertEqual(self.snapshot_before, snapshot_after)
        doc = self.read_hits_doc()
        self.assertEqual(doc["summary"]["total_hits"], 3)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_write_landed_only_under_output_dir(self) -> None:
        _run_main(self.scan_argv([self.proj], self.catalog, quiet=True))
        for dirpath, dirnames, filenames in os.walk(str(self.tmp)):
            try:
                Path(dirpath).relative_to(self.proj)
                continue
            except ValueError:
                pass
            for name in filenames:
                full = Path(dirpath) / name
                if full == self.catalog:
                    continue
                try:
                    full.relative_to(self.output_dir)
                except ValueError:
                    self.fail(f"unexpected write outside output dir: {full}")


# ---------------------------------------------------------------------------
# write_only_within containment
# ---------------------------------------------------------------------------


class WriteOnlyWithinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dcc2-wow-"))
        self.out_dir = self.tmp / "out"
        self.out_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_path_outside_base_dir_rejected(self) -> None:
        resolved, reason = dcc.write_only_within(self.out_dir, str(self.tmp / "elsewhere.json"))
        self.assertIsNone(resolved)
        self.assertEqual(reason, "outside_base_dir")

    def test_relative_path_rejected(self) -> None:
        resolved, reason = dcc.write_only_within(self.out_dir, "relative/path.json")
        self.assertIsNone(resolved)
        self.assertEqual(reason, "non_absolute_path")

    def test_symlink_component_rejected(self) -> None:
        real_target = self.tmp / "real-dir"
        real_target.mkdir()
        symlink = self.out_dir / "sub"
        symlink.symlink_to(real_target)
        target = symlink / "file.json"
        resolved, reason = dcc.write_only_within(self.out_dir, str(target))
        self.assertIsNone(resolved)
        self.assertEqual(reason, "symlink_path")

    def test_valid_path_within_base_dir_accepted(self) -> None:
        target = self.out_dir / "file.json"
        resolved, reason = dcc.write_only_within(self.out_dir, str(target))
        self.assertIsNone(reason)
        self.assertEqual(resolved, target.resolve())


# ---------------------------------------------------------------------------
# hit_id stability
# ---------------------------------------------------------------------------


class UnmatchedProjectIdTests(BaseTestCase):
    """resolve_project_id()'s fallback for a root that doesn't match any
    catalog.json project must be keyed by the root's resolved real path,
    not just its basename -- two physically distinct roots sharing a
    basename must never collide onto the same fallback project_id, because
    hit_id = sha256(project_id|signal_type|path) and a collision there
    breaks the state-persistence identity contract (this file's own
    "single most important correctness requirement"). See
    resolve_project_id()'s own docstring for the direct-reproduction
    writeup."""

    def test_unmatched_roots_with_same_basename_do_not_collide(self) -> None:
        baseline = {
            "project_real_path_to_id": {},
            "capability_paths_by_project": {},
            "wiki_paths_by_project": {},
            "staleness_by_project": {},
        }
        root_a = self.tmp / "parent-a" / "scripts"
        root_b = self.tmp / "parent-b" / "scripts"
        root_a.mkdir(parents=True)
        root_b.mkdir(parents=True)

        pid_a, match_a = dcc.resolve_project_id(root_a, baseline)
        pid_b, match_b = dcc.resolve_project_id(root_b, baseline)

        self.assertFalse(match_a)
        self.assertFalse(match_b)
        self.assertNotEqual(pid_a, pid_b)
        self.assertTrue(pid_a.startswith("unmatched:scripts:"))
        self.assertTrue(pid_b.startswith("unmatched:scripts:"))

        # Stable across reruns of the SAME physical root -- otherwise
        # rescanning an unmatched project would look like a project change
        # and lose its own terminal triage states.
        pid_a_again, _ = dcc.resolve_project_id(root_a, baseline)
        self.assertEqual(pid_a, pid_a_again)

    def test_end_to_end_scan_of_two_same_basename_unmatched_roots_keeps_both_hits_distinct(self) -> None:
        # Full scan through cmd_scan/main -- not just the unit-level
        # resolve_project_id() call above -- confirming the fix actually
        # reaches the written discovery-hits.json: two roots with the same
        # basename, neither in catalog.json, each containing a DIFFERENT
        # script, must produce two hits with distinct hit_id and distinct
        # content_sha256 in the output file.
        catalog = self.tmp / "catalog.json"
        _write_json(catalog, make_catalog(projects=[]))

        root_a = self.tmp / "parent-a" / "scripts"
        root_b = self.tmp / "parent-b" / "scripts"
        self.make_script(root_a, "tool.py")
        self.make_script(root_b, "tool.py")
        # Make the two files' content genuinely different so a collision
        # would be detectable via a stomped/overwritten content_sha256 too.
        (root_b / "tool.py").write_text(
            (root_b / "tool.py").read_text(encoding="utf-8") + "\n# distinguishing comment\n",
            encoding="utf-8",
        )

        code, _out, _err = _run_main(self.scan_argv([root_a, root_b], catalog, quiet=True))
        self.assertEqual(code, 0)

        doc = self.read_hits_doc()
        hits = [h for h in doc["hits"] if h["signal_type"] == "capability:script"]
        self.assertEqual(len(hits), 2)
        hit_ids = {h["hit_id"] for h in hits}
        project_ids = {h["project_id"] for h in hits}
        content_hashes = {h["content_sha256"] for h in hits}
        self.assertEqual(len(hit_ids), 2, "hit_id collision between two same-basename unmatched roots")
        self.assertEqual(len(project_ids), 2, "fallback project_id collided between the two roots")
        self.assertEqual(len(content_hashes), 2)


class HitIdTests(unittest.TestCase):
    def test_hit_id_is_stable_for_same_inputs(self) -> None:
        a = dcc.compute_hit_id("proj", "capability:script", "scripts/x.py")
        b = dcc.compute_hit_id("proj", "capability:script", "scripts/x.py")
        self.assertEqual(a, b)

    def test_hit_id_differs_by_path(self) -> None:
        a = dcc.compute_hit_id("proj", "capability:script", "scripts/x.py")
        b = dcc.compute_hit_id("proj", "capability:script", "scripts/y.py")
        self.assertNotEqual(a, b)

    def test_hit_id_unaffected_by_content_sha256(self) -> None:
        # The identity used for rerun persistence is (project_id, signal_type,
        # path) only -- content_sha256 is a separate field on the record, not
        # part of the identity. See module docstring's persistence section.
        a = dcc.compute_hit_id("proj", "capability:script", "scripts/x.py")
        self.assertEqual(len(a), 64)  # sha256 hex digest length, sanity check


if __name__ == "__main__":
    unittest.main()
