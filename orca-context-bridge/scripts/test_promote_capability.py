#!/usr/bin/env python3
"""Unit + isolation tests for promote_capability.py (M8-3, Gate B).

SAFETY INVARIANT THIS FILE EXISTS TO PROVE (see module docstring below and
setUpModule/tearDownModule): every single test in this file operates on
fake projects built fresh under tempfile.mkdtemp(), each with its own
independent `git init` repository -- NEVER the real "完善orca" repository
this test file itself lives in, and NEVER any other real project on this
machine. setUpModule()/tearDownModule() snapshot `git status --porcelain`
of the real repo before and after the ENTIRE test run and assert they are
byte-for-byte identical, regardless of which individual tests ran or in
what order.

Run with:
    python3 -m unittest test_promote_capability.py -v
(from this directory), or plain `python3 test_promote_capability.py`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import promote_capability as pc  # noqa: E402

# ---------------------------------------------------------------------------
# Module-wide safety net: prove the REAL repository's git state is untouched
# by this entire test run, no matter which tests execute. Read-only diff
# only -- `git status --porcelain`, never a write command.
#
# FIXED absolute path, deliberately NOT Path(__file__).resolve().parents[2]:
# this file is deployed to two locations -- the tracked workspace
# (orca-context-bridge/scripts/, three levels under the real repo root, where
# the old relative derivation happened to be correct) and
# ~/.agents/skills/orca-context-bridge/scripts/ (not a git checkout at all,
# where that same derivation resolves to something with no .git and this
# module's own setUpModule() sanity check fails before a single test can
# run). This is exactly the AUTHORITY_TRACKED_PATHS relocate-time lesson M8
# Gate A already hit once this session, applied here to a test-only
# constant rather than a production write path -- same root cause (a path
# pinned relative to wherever this file happens to be deployed, not to
# where it actually needs to point), lower stakes (this only breaks the
# test suite's own precondition check, not a real write), same fix (a fixed
# absolute path, independent of deployment location).
# ---------------------------------------------------------------------------

REAL_REPO_ROOT = Path("/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca")
_REAL_REPO_STATUS_BEFORE: str | None = None


def _real_repo_git_status() -> str:
    proc = subprocess.run(
        ["git", "-C", str(REAL_REPO_ROOT), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout


def setUpModule() -> None:
    global _REAL_REPO_STATUS_BEFORE
    assert (REAL_REPO_ROOT / ".git").exists(), f"sanity check failed: {REAL_REPO_ROOT} does not look like the real repo"
    _REAL_REPO_STATUS_BEFORE = _real_repo_git_status()


def tearDownModule() -> None:
    after = _real_repo_git_status()
    if after != _REAL_REPO_STATUS_BEFORE:
        raise AssertionError(
            "REAL REPO GIT STATE CHANGED DURING THE TEST RUN -- this must never happen.\n"
            f"--- before ---\n{_REAL_REPO_STATUS_BEFORE!r}\n--- after ---\n{after!r}"
        )


# ---------------------------------------------------------------------------
# Fixture helpers -- every path below is inside a tempfile.mkdtemp() tree.
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd)] + args, capture_output=True, text=True, timeout=30, check=True)


def make_fake_project(
    base_dir: Path,
    project_id: str,
    *,
    wiki_content_version: int = 1,
    with_manifest_pin: bool = False,
    add_script_file: str | None = "scripts/my_script.py",
) -> Path:
    """An independent fake project: its own `git init` repo, its own fake
    wiki/*.json files. NEVER a subdirectory of the real repo."""
    proj_dir = base_dir / "projects" / project_id.replace("/", "__")
    proj_dir.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], proj_dir)
    _run_git(["config", "user.email", "test@example.invalid"], proj_dir)
    _run_git(["config", "user.name", "Promote Capability Test"], proj_dir)

    wiki_dir = proj_dir / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    reusable = {"schema_version": 1, "project": project_id, "capabilities": []}
    (wiki_dir / "reusable-capabilities.json").write_text(json.dumps(reusable, indent=2, ensure_ascii=False), encoding="utf-8")
    wiki_doc = {
        "version": 1,
        "meta": {"content_version": wiki_content_version, "updated_at": "2026-08-01T00:00:00Z"},
        "project": {"path": str(proj_dir)},
        "pages": [],
    }
    (wiki_dir / "orca-context-wiki.json").write_text(json.dumps(wiki_doc, indent=2, ensure_ascii=False), encoding="utf-8")

    if add_script_file:
        script_path = proj_dir / add_script_file
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text("#!/usr/bin/env python3\n# dummy fixture script\n", encoding="utf-8")

    if with_manifest_pin:
        manifest_dir = proj_dir / ".orca" / "context"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "reviewed-startup-pack-manifest.json").write_text(
            json.dumps({"schema_version": 2, "shared_source_sha256s": {"wiki": "0" * 64}}, indent=2),
            encoding="utf-8",
        )

    _run_git(["add", "-A"], proj_dir)
    _run_git(["commit", "-q", "-m", "fixture: initial fake project state"], proj_dir)
    return proj_dir


def _chmod_tree(path: Path, mode: int) -> None:
    for root, dirs, files in os.walk(path):
        for name in dirs:
            os.chmod(os.path.join(root, name), mode)
        for name in files:
            os.chmod(os.path.join(root, name), mode)
    os.chmod(str(path), mode)


def make_catalog_file(
    catalog_path: Path,
    projects: dict[str, Path],
    *,
    capability_ref_index: dict[str, str] | None = None,
) -> None:
    doc = {
        "projects": [{"project_id": pid, "real_path": str(path), "status": "ok"} for pid, path in projects.items()],
        "capabilities": [],
        "capability_ref_index": capability_ref_index or {},
    }
    catalog_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pc.main(argv)
    return code, out.getvalue(), err.getvalue()


def run_cli_json(argv: list[str]) -> tuple[int, dict, str]:
    code, out, err = run_cli(argv + ["--json"])
    # Success prints its JSON body to stdout; _emit_error() prints its JSON
    # body to stderr (see promote_capability.py's _emit_error). Fall back
    # to stderr so callers get a real "reason" either way instead of {}.
    body = out.strip() or err.strip()
    parsed = json.loads(body) if body else {}
    return code, parsed, err


# ---------------------------------------------------------------------------
# Base test case: isolated tmp tree + isolated PROMOTION_ROOT (module
# constant monkeypatch -- promote_capability.py deliberately exposes no CLI
# flag to override it, matching detect_capability_changes.py's own
# established convention; see that script's module docstring).
# ---------------------------------------------------------------------------


class PromoteCapabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="promote-cap-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.promotion_root = self.tmp / "promotion-root"
        self._orig_root = pc.PROMOTION_ROOT
        pc.PROMOTION_ROOT = self.promotion_root
        self.addCleanup(self._restore_root)
        self.catalog_path = self.tmp / "catalog.json"

    def _restore_root(self) -> None:
        pc.PROMOTION_ROOT = self._orig_root

    def make_project(self, project_id: str, **kwargs) -> Path:
        return make_fake_project(self.tmp, project_id, **kwargs)

    def write_catalog(self, projects: dict[str, Path], **kwargs) -> None:
        make_catalog_file(self.catalog_path, projects, **kwargs)

    def draft_capability(self, project_id: str, **overrides) -> tuple[int, dict, str]:
        payload = {
            "target_project": project_id,
            "proposed_target": "reusable-capabilities.json",
            "id": "my-cap",
            "kind": "script",
            "name": "my_script.py",
            "path": "scripts/my_script.py",
            "summary": "A dummy capability for tests.",
            "last_verified_at": None,
            "depends_on": [],
            "source": {"mechanism": "human"},
        }
        payload.update(overrides)
        input_path = self.tmp / f"draft-input-{len(list(self.tmp.glob('draft-input-*')))}.json"
        input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return run_cli_json(["draft", "--from-json", str(input_path), "--catalog", str(self.catalog_path)])

    def draft_knowledge(self, project_id: str, **overrides) -> tuple[int, dict, str]:
        payload = {
            "target_project": project_id,
            "proposed_target": "orca-context-wiki.json",
            "id": "x-algo-notes",
            "title": "X 算法笔记",
            "path": "wiki/knowledge/x-algo-notes.md",
            "summary": "Notes about the X algorithm.",
            "wiki_status": "promoted-unverified",
            "content_md": "# X 算法笔记\n\n正文内容。\n",
            "source": {"mechanism": "human"},
        }
        payload.update(overrides)
        input_path = self.tmp / f"draft-input-{len(list(self.tmp.glob('draft-input-*')))}.json"
        input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return run_cli_json(["draft", "--from-json", str(input_path), "--catalog", str(self.catalog_path)])


# ---------------------------------------------------------------------------
# Unit-level tests: helpers, dedup algorithm, input validation
# ---------------------------------------------------------------------------


class NormalizeAndHashTests(unittest.TestCase):
    def test_normalize_is_nfc_casefold(self) -> None:
        # "Å" (U+00C5) vs "A" + combining ring (U+0041 U+030A) -- same
        # grapheme, different code points until NFC-normalized.
        composed = "Å"
        decomposed = "Å"
        self.assertEqual(pc._normalize(composed), pc._normalize(decomposed))
        self.assertEqual(pc._normalize("ABC"), pc._normalize("abc"))

    def test_compute_key_deterministic(self) -> None:
        fields = {"kind": "script", "name": "foo.py", "title": None}
        k1 = pc.compute_key("proj-a", "reusable-capabilities.json", fields)
        k2 = pc.compute_key("proj-a", "reusable-capabilities.json", fields)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)

    def test_compute_key_sensitive_to_project_kind_name(self) -> None:
        base = pc.compute_key("proj-a", "reusable-capabilities.json", {"kind": "script", "name": "foo.py", "title": None})
        other_project = pc.compute_key("proj-b", "reusable-capabilities.json", {"kind": "script", "name": "foo.py", "title": None})
        other_name = pc.compute_key("proj-a", "reusable-capabilities.json", {"kind": "script", "name": "bar.py", "title": None})
        knowledge = pc.compute_key("proj-a", "orca-context-wiki.json", {"kind": None, "name": None, "title": "foo.py"})
        self.assertNotEqual(base, other_project)
        self.assertNotEqual(base, other_name)
        # Same literal name string, but knowledge vs capability namespace
        # must not collide (design's kind_label discipline).
        self.assertNotEqual(base, knowledge)

    def test_compute_key_case_and_form_insensitive(self) -> None:
        a = pc.compute_key("Proj-A", "reusable-capabilities.json", {"kind": "script", "name": "FOO.py", "title": None})
        b = pc.compute_key("proj-a", "reusable-capabilities.json", {"kind": "script", "name": "foo.py", "title": None})
        self.assertEqual(a, b)

    def test_compute_content_hash_sensitive_to_summary_and_body(self) -> None:
        h1 = pc.compute_content_hash({"summary": "a summary", "content_md": None})
        h2 = pc.compute_content_hash({"summary": "a different summary", "content_md": None})
        h3 = pc.compute_content_hash({"summary": "a summary", "content_md": "body text"})
        self.assertNotEqual(h1, h2)
        self.assertNotEqual(h1, h3)


class KindValuesCrossFileEqualityTests(unittest.TestCase):
    """§3.0.2's new mandate: any 'copy, don't import' constant gets a
    cross-file equality test, not just a one-time assertion in prose. This
    is exactly the failure class that already bit this repo for real
    (agent_capacity.py's two diverged copies)."""

    def test_kind_values_matches_validate_reusable_capabilities(self) -> None:
        scripts_dir = Path(__file__).resolve().parent
        sys.path.insert(0, str(scripts_dir))
        import validate_reusable_capabilities as vrc  # noqa: E402

        self.assertEqual(tuple(pc.KIND_VALUES), tuple(vrc.KIND_VALUES))

    def test_kind_values_matches_build_cross_project_catalog(self) -> None:
        # build_cross_project_catalog.py is a real sibling of this test
        # file in this repo's scripts/ dir; prefer that, falling back to
        # the installed skill copy for environments where only the
        # installed copy is present. Skip gracefully if neither is found
        # rather than failing a test on an unrelated deployment gap.
        candidate_paths = [
            Path(__file__).resolve().parent / "build_cross_project_catalog.py",
            Path.home() / ".agents" / "skills" / "orca-context-bridge" / "scripts" / "build_cross_project_catalog.py",
        ]
        found = next((p for p in candidate_paths if p.is_file()), None)
        if found is None:
            self.skipTest("build_cross_project_catalog.py not found on this machine; skipping cross-file check")
        text = found.read_text(encoding="utf-8")
        self.assertIn('KIND_VALUES = ("skill", "script", "config-pattern")', text)


class CapabilityCandidateInputValidationTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        fields = pc.validate_capability_candidate_input(
            {
                "id": "my-cap",
                "kind": "script",
                "name": "my_script.py",
                "path": "scripts/my_script.py",
                "summary": "does a thing",
                "last_verified_at": None,
                "depends_on": [],
            }
        )
        self.assertEqual(fields["id"], "my-cap")
        self.assertEqual(fields["kind"], "script")

    def test_bad_kind_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_capability_candidate_input(
                {"id": "x", "kind": "bogus", "name": "n", "path": "p.py", "summary": "s"}
            )

    def test_bad_id_grammar_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_capability_candidate_input(
                {"id": "Not_Valid", "kind": "script", "name": "n.py", "path": "p.py", "summary": "s"}
            )

    def test_path_traversal_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_capability_candidate_input(
                {"id": "x", "kind": "script", "name": "n.py", "path": "../../etc/passwd", "summary": "s"}
            )

    def test_absolute_path_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_capability_candidate_input(
                {"id": "x", "kind": "script", "name": "n.py", "path": "/etc/passwd", "summary": "s"}
            )

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_capability_candidate_input(
                {"id": "x", "kind": "script", "name": "n.py", "path": "p.py", "summary": "s", "bogus_field": 1}
            )

    def test_depends_on_self_reference_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_capability_candidate_input(
                {
                    "id": "x",
                    "kind": "script",
                    "name": "n.py",
                    "path": "p.py",
                    "summary": "s",
                    "depends_on": ["script:n.py"],
                }
            )

    def test_depends_on_bad_grammar_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_capability_candidate_input(
                {"id": "x", "kind": "script", "name": "n.py", "path": "p.py", "summary": "s", "depends_on": ["not-valid"]}
            )


class KnowledgeCandidateInputValidationTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        fields = pc.validate_knowledge_candidate_input(
            {"id": "notes", "title": "Some Notes", "path": "wiki/knowledge/notes.md", "summary": "notes about x"}
        )
        self.assertEqual(fields["title"], "Some Notes")
        self.assertEqual(fields["wiki_status"], "promoted-unverified")

    def test_depends_on_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_knowledge_candidate_input(
                {
                    "id": "notes",
                    "title": "t",
                    "path": "p.md",
                    "summary": "s",
                    "depends_on": ["script:a.py"],
                }
            )

    def test_content_md_too_large_rejected(self) -> None:
        with self.assertRaises(pc.PromoteValidationError):
            pc.validate_knowledge_candidate_input(
                {
                    "id": "notes",
                    "title": "t",
                    "path": "p.md",
                    "summary": "s",
                    "content_md": "x" * (pc.MAX_CONTENT_MD_BYTES + 1),
                }
            )


# ---------------------------------------------------------------------------
# CLI: draft
# ---------------------------------------------------------------------------


class DraftCommandTests(PromoteCapabilityTestCase):
    def test_draft_capability_candidate_creates_pending_record(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_capability("proj-a")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "pending_approval")
        candidate_file = self.promotion_root / "proj-a" / f"{result['candidate_id']}.json"
        self.assertTrue(candidate_file.is_file())
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        self.assertEqual(record["operation"], "create")
        self.assertEqual(record["proposed_target"], "reusable-capabilities.json")

    def test_draft_knowledge_candidate_stages_content_md(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_knowledge("proj-a")
        self.assertEqual(code, 0, result)
        content_file = self.promotion_root / "proj-a" / result["candidate_id"] / "content.md"
        self.assertTrue(content_file.is_file())
        self.assertIn("正文内容", content_file.read_text(encoding="utf-8"))

    def test_draft_exact_duplicate_rejected(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code1, result1, _ = self.draft_capability("proj-a")
        self.assertEqual(code1, 0)
        code2, result2, _ = self.draft_capability("proj-a")
        self.assertEqual(code2, 1)
        self.assertEqual(result2["reason"], "exact_duplicate")
        self.assertEqual(result2["message"]["existing_candidate_id"], result1["candidate_id"])

    def test_draft_content_change_marks_possible_revision(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code1, result1, _ = self.draft_capability("proj-a", summary="first summary text")
        self.assertEqual(code1, 0)
        code2, result2, _ = self.draft_capability("proj-a", summary="a completely different summary text")
        self.assertEqual(code2, 0)
        self.assertEqual(result2["possible_revision_of"], result1["candidate_id"])
        self.assertNotEqual(result1["content_hash"], result2["content_hash"])
        self.assertEqual(result1["key"], result2["key"])

    def test_draft_bad_json_input_is_usage_error(self) -> None:
        bad_path = self.tmp / "bad.json"
        bad_path.write_text("{not valid json", encoding="utf-8")
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = run_cli_json(["draft", "--from-json", str(bad_path), "--catalog", str(self.catalog_path)])
        self.assertEqual(code, 2)

    def test_draft_missing_input_file_is_usage_error(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = run_cli_json(
            ["draft", "--from-json", str(self.tmp / "does-not-exist.json"), "--catalog", str(self.catalog_path)]
        )
        self.assertEqual(code, 2)

    def test_draft_bad_proposed_target_is_validation_error(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_capability("proj-a", proposed_target="something-else.json")
        self.assertEqual(code, 1)

    def test_draft_unknown_target_project_is_recorded_not_blocked(self) -> None:
        self.write_catalog({})
        code, result, _err = self.draft_capability("totally-unknown-project")
        self.assertEqual(code, 0)
        self.assertFalse(result["target_project_known_in_catalog"])

    def test_draft_depends_on_resolution_against_catalog(self) -> None:
        proj_a = self.make_project("proj-a")
        proj_b = self.make_project("proj-b")
        self.write_catalog(
            {"proj-a": proj_a, "proj-b": proj_b},
            capability_ref_index={"proj-a:script:base.py": "proj-a#base-cap"},
        )
        code, result, _err = self.draft_capability(
            "proj-a", name="dependent.py", id="dependent-cap", depends_on=["script:base.py", "script:missing.py"]
        )
        self.assertEqual(code, 0, result)
        resolutions = {r["raw"]: r for r in result["depends_on_resolution"]}
        self.assertTrue(resolutions["script:base.py"]["resolved"])
        self.assertFalse(resolutions["script:missing.py"]["resolved"])

    def test_draft_target_project_path_traversal_rejected(self) -> None:
        self.write_catalog({})
        code, result, _err = self.draft_capability("../../etc")
        self.assertEqual(code, 1)


# ---------------------------------------------------------------------------
# CLI: approve -- reusable-capabilities.json branch
# ---------------------------------------------------------------------------


class ApproveReusableCapabilitiesTests(PromoteCapabilityTestCase):
    def _draft_and_get_id(self, project_id: str, **overrides) -> str:
        code, result, _err = self.draft_capability(project_id, **overrides)
        self.assertEqual(code, 0, result)
        return result["candidate_id"]

    def test_approve_writes_into_correct_fake_project_only(self) -> None:
        proj_a = self.make_project("proj-a")
        proj_b = self.make_project("proj-b")
        self.write_catalog({"proj-a": proj_a, "proj-b": proj_b})
        candidate_id = self._draft_and_get_id("proj-a")

        code, result, _err = run_cli_json(
            [
                "approve",
                "--candidate-id",
                candidate_id,
                "--catalog",
                str(self.catalog_path),
                "--approved-by",
                "tester",
                "--rationale",
                "looks good",
            ]
        )
        self.assertEqual(code, 0, result)

        doc_a = json.loads((proj_a / "wiki" / "reusable-capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(len(doc_a["capabilities"]), 1)
        self.assertEqual(doc_a["capabilities"][0]["id"], "my-cap")

        doc_b = json.loads((proj_b / "wiki" / "reusable-capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(doc_b["capabilities"], [])

    def test_approve_updates_candidate_status_and_ledger(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "tester", "--rationale", "ok"]
        )
        self.assertEqual(code, 0, result)
        record = json.loads((self.promotion_root / "proj-a" / f"{candidate_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["approved_by"], "tester")
        ledger_lines = (self.promotion_root / pc.LEDGER_NAME).read_text(encoding="utf-8").strip().splitlines()
        actions = [json.loads(line)["action"] for line in ledger_lines]
        self.assertEqual(actions, ["draft", "approve"])

    def test_approve_rolls_back_when_referenced_path_missing(self) -> None:
        # draft's own path validation is structural-only (no existence
        # check); approve's post-write self-check DOES check existence
        # (check_paths=True) -- this is the gap that makes a genuine
        # rollback reachable without any mocking.
        proj = self.make_project("proj-a", add_script_file=None)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a", path="scripts/does_not_exist.py")

        before = (proj / "wiki" / "reusable-capabilities.json").read_bytes()
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 1, result)
        self.assertEqual(result["reason"], "post_write_self_check_failed")
        after = (proj / "wiki" / "reusable-capabilities.json").read_bytes()
        self.assertEqual(before, after, "file must be rolled back to its original bytes")

    def test_approve_duplicate_id_in_target_rejected(self) -> None:
        proj = self.make_project("proj-a")
        doc_path = proj / "wiki" / "reusable-capabilities.json"
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        doc["capabilities"].append(
            {
                "id": "my-cap",
                "kind": "script",
                "name": "other.py",
                "path": "scripts/my_script.py",
                "summary": "already exists",
                "last_verified_at": None,
                "depends_on": [],
            }
        )
        doc_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _run_git(["add", "-A"], proj)
        _run_git(["commit", "-q", "-m", "seed duplicate id"], proj)

        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "duplicate_id")

    def test_approve_concurrent_modification_detected(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        before = (proj / "wiki" / "reusable-capabilities.json").read_bytes()

        with mock.patch.object(pc, "identity_unchanged", return_value=False):
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "concurrent_modification_detected")
        after = (proj / "wiki" / "reusable-capabilities.json").read_bytes()
        self.assertEqual(before, after, "no write should happen when concurrency check fails")

    def test_approve_candidate_not_found(self) -> None:
        self.write_catalog({})
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", "cand-doesnotexist", "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "candidate_not_found")

    def test_approve_twice_rejected_second_time(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code1, _r1, _e1 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code1, 0)
        code2, result2, _e2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code2, 1)
        self.assertEqual(result2["reason"], "candidate_not_pending")

    def test_approve_missing_approved_by_is_usage_error(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "  ", "--rationale", "r"]
        )
        self.assertEqual(code, 2)

    def test_approve_non_human_source_requires_confirm_flag(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a", source={"mechanism": "auto-scan"})
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["reason"], "confirm_non_human_source_required")

        code2, result2, _e2 = run_cli(
            [
                "approve",
                "--candidate-id",
                candidate_id,
                "--catalog",
                str(self.catalog_path),
                "--approved-by",
                "t",
                "--rationale",
                "r",
                "--confirm-non-human-source",
                "--json",
            ]
        )
        self.assertEqual(code2, 0)

    def test_approve_unknown_target_project_in_catalog_is_fatal(self) -> None:
        proj = self.make_project("proj-a")
        # catalog omits proj-a entirely by the time approve runs
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        self.write_catalog({})
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 4)
        self.assertEqual(result["reason"], "target_project_unknown_in_catalog")

    def test_wiki_dir_symlink_swap_before_dir_fd_open_fails_closed(self) -> None:
        """2026-08-26, 3-model max-effort cross-audit finding: this
        function's write used to call atomic_write_in_dir(wiki_path,
        new_bytes) with NO dir_fd -- unlike ApproveOrcaContextWikiTests'
        sibling approval path, which got the round-5/round-6 TOCTOU
        hardening for the exact same class of write. Unlike that sibling
        path, this one has no subprocess spawn between the dir-fd open and
        the write (the write happens synchronously, in-process, right
        after) -- so there is no equivalent "spawn latency" residual
        window to target. The earliest an attacker could plausibly race is
        immediately before _open_wiki_dir_fd_for_guard()'s own os.open()
        call; swap real_path/wiki for a symlink to an outside decoy right
        there (by hooking that real function's entry, before it calls the
        real os.open()) and confirm O_NOFOLLOW refuses it outright rather
        than following it -- the decoy must never be touched, and approve
        must report a clean, named failure, not a false success."""
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        wiki_dir = proj / "wiki"
        before_bytes = (wiki_dir / "reusable-capabilities.json").read_bytes()
        outside_dir = self.tmp / "outside-decoy-reusable-caps"
        outside_dir.mkdir()
        outside_copy = outside_dir / "reusable-capabilities.json"
        outside_copy.write_bytes(before_bytes)
        moved_aside = proj / "wiki-real-reusable-caps"

        real_open = pc._open_wiki_dir_fd_for_guard
        state = {"done": False}

        def swap_then_open(real_path):
            if not state["done"]:
                state["done"] = True
                os.rename(str(wiki_dir), str(moved_aside))
                wiki_dir.symlink_to(outside_dir, target_is_directory=True)
            return real_open(real_path)

        pc._open_wiki_dir_fd_for_guard = swap_then_open
        try:
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        finally:
            pc._open_wiki_dir_fd_for_guard = real_open

        self.assertEqual(outside_copy.read_bytes(), before_bytes, "the outside decoy copy must never be mutated")
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "wiki_dir_unopenable_for_guard")
        # Nothing was written anywhere -- the original (now moved-aside)
        # file is untouched too.
        real_doc = json.loads((moved_aside / "reusable-capabilities.json").read_bytes().decode("utf-8"))
        self.assertEqual(real_doc["capabilities"], [])

    def test_wiki_dir_real_directory_swap_bounded_not_escape(self) -> None:
        """Companion to the symlink-swap test above: a brand-new REAL
        (non-symlink) directory swapped in for wiki/ cannot be caught by
        O_NOFOLLOW (it isn't a symlink) -- the open succeeds against the
        substitute, and the write lands there. That is bounded, not an
        escape, since the substitute was created as real_path/"wiki" by
        construction -- same accepted shape as the wiki-approval path's
        own documented residual for this exact scenario."""
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        wiki_dir = proj / "wiki"
        before_bytes = (wiki_dir / "reusable-capabilities.json").read_bytes()
        moved_aside = proj / "wiki-original-moved-reusable-caps"

        real_open = pc._open_wiki_dir_fd_for_guard
        state = {"done": False}

        def swap_then_open(real_path):
            if not state["done"]:
                state["done"] = True
                os.rename(str(wiki_dir), str(moved_aside))
                wiki_dir.mkdir()
                (wiki_dir / "reusable-capabilities.json").write_bytes(before_bytes)
            return real_open(real_path)

        pc._open_wiki_dir_fd_for_guard = swap_then_open
        try:
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        finally:
            pc._open_wiki_dir_fd_for_guard = real_open

        self.assertEqual(code, 0, result)
        original_doc = json.loads((moved_aside / "reusable-capabilities.json").read_bytes().decode("utf-8"))
        substituted_doc = json.loads((wiki_dir / "reusable-capabilities.json").read_bytes().decode("utf-8"))
        self.assertEqual(original_doc["capabilities"], [], "original moved-aside copy must stay unchanged")
        self.assertEqual(len(substituted_doc["capabilities"]), 1)
        self.assertEqual(substituted_doc["capabilities"][0]["id"], "my-cap")
        self.assertTrue(str(wiki_dir.resolve()).startswith(str(proj.resolve())), "substituted dir still inside project")

    def test_post_write_self_check_reads_via_dir_fd_not_plain_path(self) -> None:
        """2026-08-26, 3-model max-effort cross-audit DISPUTE over this same
        function: Codex rated this P1 (a wiki/ swap between the dir-fd-
        anchored write completing and the post-write self-check's re-read
        can make the self-check validate a decoy instead of what was
        actually written, producing a false "approved successfully"
        report while the real inode holds the real bytes, now orphaned
        under the pre-swap name); Grok agreed on the mechanism but called
        it P2 and folded it into the rollback-reporting issue below;
        Gemini asserted this function's dir-fd fix already closes every
        TOCTOU gap here with no residual issue. Adjudicated by reproducing
        it directly: the write's OWN dir_fd anchoring does hold (confirmed
        below -- the real write lands correctly in the pre-swap directory,
        which is exactly why that data survives at moved_aside), but the
        self-check that ran right after it, pre-fix, used
        wiki_path.read_bytes() -- a fresh plain-path walk of "wiki" that
        the swap below redirects to an attacker-controlled decoy -- so
        Codex/Grok's mechanism was real, not Gemini's "no issue". Pre-fix
        this produced exit 0 (false success) with the decoy silently
        substituted for validation. Fixed by (a) reading the self-check's
        bytes through the SAME dir_fd the write used (immune to the name
        swap), and (b) an explicit post-write check that "wiki" still
        resolves to the fd's own directory, so a hijack mid-operation is
        refused outright rather than validated-via-fd and silently
        accepted as success."""
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        wiki_dir = proj / "wiki"
        decoy_dir = self.tmp / "decoy-after-write"
        decoy_dir.mkdir()
        # Must independently pass validate_document() (schema_version +
        # project + capabilities, all required top-level keys) so a
        # pre-fix self-check that reads THIS instead of the real write
        # finds no errors at all and reports a bare false success --
        # not merely a different, but still-detected, validation failure.
        decoy_doc = {"schema_version": 1, "project": "proj-a", "capabilities": []}
        (decoy_dir / "reusable-capabilities.json").write_text(json.dumps(decoy_doc, indent=2) + "\n", encoding="utf-8")
        moved_aside = proj / "wiki-real-after-write"

        real_atomic_write = pc.atomic_write_in_dir
        state = {"swapped": False}

        def swap_right_after_write(final_path, payload, *, dir_fd=None):
            real_atomic_write(final_path, payload, dir_fd=dir_fd)
            if not state["swapped"] and final_path.name == "reusable-capabilities.json" and dir_fd is not None:
                state["swapped"] = True
                os.rename(str(wiki_dir), str(moved_aside))
                wiki_dir.symlink_to(decoy_dir, target_is_directory=True)

        pc.atomic_write_in_dir = swap_right_after_write
        try:
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        finally:
            pc.atomic_write_in_dir = real_atomic_write

        # The real dir_fd-anchored write DID land correctly in the
        # pre-swap directory (now orphaned at moved_aside) -- proving the
        # write side's own anchoring holds regardless of this bug.
        real_written_doc = json.loads((moved_aside / "reusable-capabilities.json").read_bytes().decode("utf-8"))
        self.assertEqual(len(real_written_doc["capabilities"]), 1, "the real dir_fd-anchored write must have landed correctly")

        # Post-fix: the hijack is detected and refused -- NOT a false
        # success against the decoy (the pre-fix behavior).
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "concurrent_modification_detected")
        # The decoy must never be mutated (nothing should ever write
        # through the hijacked name).
        self.assertEqual(
            json.loads(decoy_dir.joinpath("reusable-capabilities.json").read_bytes().decode("utf-8")),
            decoy_doc,
        )

    def test_rollback_skipped_by_symlink_guard_still_reports_rolled_back_false(self) -> None:
        """Second half of the same 2026-08-26 cross-audit dispute: on the
        rollback branch (self-check finds real validation errors), if
        wiki/ has meanwhile become a symlink, _has_symlink_component()
        correctly skips the rollback write (failing closed rather than
        writing rollback bytes through an attacker-controlled symlink) --
        but pre-fix, the exception raised right after unconditionally set
        "rolled_back": True regardless of whether that guard actually let
        the rollback run. Reproduced here (not mocked-to-assume-the-
        answer): the self-check must first see the REAL, invalid written
        content to fail validation naturally -- same no-mocking-the-answer
        technique as test_approve_rolls_back_when_referenced_path_missing
        -- and only THEN, in the narrow gap before the rollback branch's
        own _has_symlink_component() re-check, does wiki/ get swapped;
        hooked via validate_document(), the exact seam between those two
        points."""
        proj = self.make_project("proj-a", add_script_file=None)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a", path="scripts/does_not_exist.py")

        wiki_dir = proj / "wiki"
        before_bytes = (wiki_dir / "reusable-capabilities.json").read_bytes()
        moved_aside = proj / "wiki-real-rollback-test"
        decoy_dir = self.tmp / "decoy-for-rollback-test"
        decoy_dir.mkdir()
        (decoy_dir / "reusable-capabilities.json").write_bytes(b'{"capabilities": []}\n')

        real_validate_document = pc.validate_document
        state = {"swapped": False}

        def swap_after_self_check_read(doc, **kwargs):
            run = real_validate_document(doc, **kwargs)
            if not state["swapped"] and run.errors:
                state["swapped"] = True
                os.rename(str(wiki_dir), str(moved_aside))
                wiki_dir.symlink_to(decoy_dir, target_is_directory=True)
            return run

        pc.validate_document = swap_after_self_check_read
        try:
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        finally:
            pc.validate_document = real_validate_document

        self.assertEqual(code, 1, result)
        self.assertEqual(result["reason"], "post_write_self_check_failed")
        # BUG (pre-fix): rolled_back was unconditionally True even though
        # the symlink guard skipped the rollback write entirely.
        self.assertFalse(result["message"]["rolled_back"], "must not claim a rollback happened when the guard skipped it")
        # Prove no rollback write actually happened: the moved-aside real
        # file (holding the invalid written content) was never restored to
        # old_raw, and the swapped-in symlink's target was never touched
        # either.
        self.assertNotEqual((moved_aside / "reusable-capabilities.json").read_bytes(), before_bytes)
        self.assertEqual((decoy_dir / "reusable-capabilities.json").read_bytes(), b'{"capabilities": []}\n')


# ---------------------------------------------------------------------------
# CLI: approve -- orca-context-wiki.json branch
# ---------------------------------------------------------------------------


class ApproveOrcaContextWikiTests(PromoteCapabilityTestCase):
    def _draft_and_get_id(self, project_id: str, **overrides) -> str:
        code, result, _err = self.draft_knowledge(project_id, **overrides)
        self.assertEqual(code, 0, result)
        return result["candidate_id"]

    def test_approve_writes_page_bumps_version_and_commits(self) -> None:
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "tester", "--rationale", "ok"]
        )
        self.assertEqual(code, 0, result)
        self.assertEqual(result["new_content_version"], 2)
        self.assertTrue(result["git_committed"])
        self.assertIsNotNone(result["git_commit_sha"])

        wiki_doc = json.loads((proj / "wiki" / "orca-context-wiki.json").read_text(encoding="utf-8"))
        self.assertEqual(wiki_doc["meta"]["content_version"], 2)
        self.assertEqual(len(wiki_doc["pages"]), 1)
        self.assertEqual(wiki_doc["pages"][0]["id"], "x-algo-notes")

        knowledge_md = proj / "wiki" / "knowledge" / "x-algo-notes.md"
        self.assertTrue(knowledge_md.is_file())
        self.assertIn("正文内容", knowledge_md.read_text(encoding="utf-8"))

        # The commit really happened in the FAKE project's own repo.
        log = subprocess.run(
            ["git", "-C", str(proj), "log", "-1", "--name-only", "--pretty=format:%s"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("wiki/orca-context-wiki.json", log)
        self.assertIn("wiki/knowledge/x-algo-notes.md", log)
        status = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(status.strip(), "", "fake project working tree must be clean after a committed approve")

    def test_approve_without_content_md_only_writes_metadata(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a", content_md=None)
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 0, result)
        self.assertIsNone(result["knowledge_md_relpath"])
        self.assertFalse((proj / "wiki" / "knowledge").exists())

    def test_approve_manifest_resign_notice_present_when_pinned(self) -> None:
        proj = self.make_project("proj-a", with_manifest_pin=True)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 0, result)
        self.assertTrue(result["requires_manifest_resign"])
        self.assertIn("SessionStart", _err)
        self.assertIn("re-sign", _err)

    def test_approve_manifest_resign_notice_absent_when_not_pinned(self) -> None:
        proj = self.make_project("proj-a", with_manifest_pin=False)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code, result, err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 0, result)
        self.assertFalse(result["requires_manifest_resign"])
        self.assertNotIn("SessionStart", err)

    def test_approve_no_commit_requires_ack_flag(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code, result, _err = run_cli_json(
            [
                "approve",
                "--candidate-id",
                candidate_id,
                "--catalog",
                str(self.catalog_path),
                "--approved-by",
                "t",
                "--rationale",
                "r",
                "--no-commit",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["reason"], "no_commit_requires_acknowledgement")

    def test_approve_no_commit_with_ack_skips_git(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code, result, err = run_cli_json(
            [
                "approve",
                "--candidate-id",
                candidate_id,
                "--catalog",
                str(self.catalog_path),
                "--approved-by",
                "t",
                "--rationale",
                "r",
                "--no-commit",
                "--i-understand-this-leaves-an-uncommitted-tracked-path",
            ]
        )
        self.assertEqual(code, 0, result)
        self.assertFalse(result["git_committed"])
        status = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertNotEqual(status.strip(), "", "wiki file should be a real uncommitted change")
        self.assertIn("uncommitted tracked-path", err)

    def test_approve_git_commit_failure_still_reports_success_with_notice(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        git_dir = proj / ".git"
        _chmod_tree(git_dir, 0o500)
        self.addCleanup(_chmod_tree, git_dir, 0o700)
        try:
            code, result, err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        finally:
            _chmod_tree(git_dir, 0o700)
        self.assertEqual(code, 0, result)
        self.assertFalse(result["git_committed"])
        self.assertIsNotNone(result.get("git_commit_error"))
        self.assertIn("uncommitted tracked-path", err)
        # The wiki write itself DID succeed even though commit failed.
        wiki_doc = json.loads((proj / "wiki" / "orca-context-wiki.json").read_text(encoding="utf-8"))
        self.assertEqual(wiki_doc["meta"]["content_version"], 2)

    def test_approve_target_not_git_repo_refuses_before_write(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        shutil.rmtree(proj / ".git")
        before = (proj / "wiki" / "orca-context-wiki.json").read_bytes()
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "target_project_not_a_git_repo")
        after = (proj / "wiki" / "orca-context-wiki.json").read_bytes()
        self.assertEqual(before, after)

    def test_approve_wiki_edit_guard_refusal_rolls_back(self) -> None:
        # old meta.updated_at set in the far future so wiki_edit_guard.py's
        # own "strictly newer" rule refuses the write for real (no
        # mocking of the guard's logic itself).
        proj = self.make_project("proj-a")
        wiki_path = proj / "wiki" / "orca-context-wiki.json"
        doc = json.loads(wiki_path.read_text(encoding="utf-8"))
        doc["meta"]["updated_at"] = "2099-01-01T00:00:00Z"
        wiki_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _run_git(["add", "-A"], proj)
        _run_git(["commit", "-q", "-m", "seed future updated_at"], proj)

        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        before = wiki_path.read_bytes()
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 1, result)
        self.assertEqual(result["reason"], "wiki_edit_guard_refused")
        after = wiki_path.read_bytes()
        self.assertEqual(before, after, "guard refusal must leave the file untouched")

    def test_approve_duplicate_page_id_rejected(self) -> None:
        proj = self.make_project("proj-a")
        wiki_path = proj / "wiki" / "orca-context-wiki.json"
        doc = json.loads(wiki_path.read_text(encoding="utf-8"))
        doc["pages"].append({"id": "x-algo-notes", "title": "existing", "path": "p.md", "summary": "s", "status": "live-verified"})
        wiki_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _run_git(["add", "-A"], proj)
        _run_git(["commit", "-q", "-m", "seed duplicate page id"], proj)

        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")
        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "duplicate_page_id")

    def test_wiki_edit_guard_hash_pin_mismatch_refuses(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        guard_path = pc.resolve_wiki_edit_guard_path()
        real_read_bytes = Path.read_bytes
        call_state = {"n": 0}

        def fake_read_bytes(self_path):  # noqa: ANN001
            if self_path == guard_path:
                call_state["n"] += 1
                if call_state["n"] == 1:
                    return real_read_bytes(self_path)
                return b"# tampered content, not the real guard\n"
            return real_read_bytes(self_path)

        before = (proj / "wiki" / "orca-context-wiki.json").read_bytes()
        with mock.patch.object(Path, "read_bytes", fake_read_bytes):
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "wiki_edit_guard_hash_mismatch")
        after = (proj / "wiki" / "orca-context-wiki.json").read_bytes()
        self.assertEqual(before, after, "no write should happen when the guard's own hash pin fails")

    def test_approve_commit_does_not_swallow_unrelated_pre_staged_file(self) -> None:
        # round-2-fix: `git commit -m msg` with no pathspec commits the
        # WHOLE index, not just what this call's own `git add` just staged.
        # A caller that had already run its own unrelated `git add` in this
        # SAME target project's working tree before invoking approve must
        # never see that file swept into approve's own commit.
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a", content_md=None)

        unrelated = proj / "UNRELATED_NOT_PART_OF_THIS_APPROVE.txt"
        unrelated.write_text("staged by someone else, unrelated to this approve call\n", encoding="utf-8")
        _run_git(["add", "--", str(unrelated)], proj)
        status_before = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertIn("UNRELATED_NOT_PART_OF_THIS_APPROVE.txt", status_before)

        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 0, result)
        self.assertTrue(result["git_committed"])

        log = subprocess.run(
            ["git", "-C", str(proj), "log", "-1", "--name-only", "--pretty=format:%s"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertNotIn(
            "UNRELATED_NOT_PART_OF_THIS_APPROVE.txt",
            log,
            "approve's own commit must never include a file the caller staged for something else",
        )
        self.assertIn("wiki/orca-context-wiki.json", log)

        # The unrelated file must still be sitting in the index afterward
        # (approve must not have unstaged it either) -- it was simply never
        # part of THIS commit.
        status_after = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertIn(
            "UNRELATED_NOT_PART_OF_THIS_APPROVE.txt",
            status_after,
            "the unrelated file must remain staged for the caller's own future commit",
        )

    def test_approve_missing_staged_content_md_leaves_wiki_byte_identical_and_repo_clean(self) -> None:
        # round-fix P0 regression: _approve_orca_context_wiki() used to check
        # whether the staged content.md still existed AFTER
        # invoke_wiki_edit_guard() had already rewritten the target
        # project's real wiki/orca-context-wiki.json on disk (appended
        # page + bumped content_version). Because that exception then
        # propagated out of the function before git_commit_paths() ever
        # ran, the mutated wiki file was left sitting on disk, modified and
        # UNCOMMITTED, in the fake project's own git tree -- exactly the
        # AUTHORITY_TRACKED_PATHS NACK shape ("an uncommitted tracked path
        # under a project's wiki/") this codebase otherwise takes care to
        # avoid. Deleting the staged content.md out from under an
        # otherwise-untampered, has_content_md=True candidate (it can
        # disappear between draft and approve for any reason) and then
        # calling approve must now be refused with NOTHING on disk ever
        # touched.
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        content_md_source = pc.content_md_path(pc.PROMOTION_ROOT, "proj-a", candidate_id)
        self.assertTrue(content_md_source.is_file())
        content_md_source.unlink()

        wiki_path = proj / "wiki" / "orca-context-wiki.json"
        before_bytes = wiki_path.read_bytes()
        before_hash = pc._sha256_hex(before_bytes)
        status_before = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(status_before.strip(), "", "fixture project must start with a clean working tree")

        code, result, _err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "staged_content_md_missing")

        # The wiki file must be COMPLETELY UNCHANGED -- not just "no page
        # added": byte-for-byte identical to before, verified via both a
        # direct comparison and an independent hash.
        after_bytes = wiki_path.read_bytes()
        self.assertEqual(before_bytes, after_bytes, "wiki file must be byte-for-byte identical, not just missing the new page")
        self.assertEqual(before_hash, pc._sha256_hex(after_bytes))

        # knowledge_final must never have been created either.
        self.assertFalse((proj / "wiki" / "knowledge").exists())

        # The fake project's git tree must show ZERO uncommitted changes --
        # this is the actual NACK-shaped signal the bug produced.
        status_after = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(status_after.strip(), "", "target project's git tree must be clean after a failed approve, never left modified-and-uncommitted")

        # The candidate record's own bookkeeping must clearly reflect that
        # approve did NOT succeed: still pending_approval, no approve_result,
        # not silently stuck in some ambiguous in-between state.
        record_path = pc.candidate_path(pc.PROMOTION_ROOT, "proj-a", candidate_id)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "pending_approval")
        self.assertNotIn("approve_result", record)

    def test_approve_knowledge_dir_permission_failure_leaves_wiki_byte_identical_and_repo_clean(self) -> None:
        # "cheap-validate-first" ordering, applied to the OTHER late check
        # this same function used to run after invoke_wiki_edit_guard():
        # resolve_knowledge_md_path() itself (containment checks + the
        # wiki/knowledge/ mkdir) also used to run only after the guard had
        # already rewritten the wiki file. Making wiki/ read-only so that
        # mkdir fails with a permission error must be refused BEFORE any
        # write to the wiki file too, for the same reason as the missing-
        # content.md case above.
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        wiki_dir = proj / "wiki"
        wiki_path = wiki_dir / "orca-context-wiki.json"
        before_bytes = wiki_path.read_bytes()

        os.chmod(str(wiki_dir), 0o500)
        self.addCleanup(os.chmod, str(wiki_dir), 0o700)
        try:
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        finally:
            os.chmod(str(wiki_dir), 0o700)

        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "target_write_permission_denied")

        after_bytes = wiki_path.read_bytes()
        self.assertEqual(before_bytes, after_bytes, "wiki file must be byte-for-byte identical after a knowledge-dir permission failure")

        status_after = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(status_after.strip(), "", "target project's git tree must be clean after a failed approve")

        record_path = pc.candidate_path(pc.PROMOTION_ROOT, "proj-a", candidate_id)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "pending_approval")

    def test_approve_preexisting_unwritable_knowledge_dir_fails_cleanly_retry_possible(self) -> None:
        # round-4-fix regression: the SECOND, more severe trigger for this
        # same bug class. Unlike the round-3 test just above (wiki/
        # ITSELF unwritable, caught early because os.makedirs() then
        # fails outright), this reproduces a pre-EXISTING wiki/knowledge/
        # directory that is itself unwritable while wiki/ remains
        # writable. os.makedirs(..., exist_ok=True) is a silent no-op on
        # an already-existing directory -- it neither chmods it nor
        # raises -- so the pre-fix code sailed straight through the
        # "cheap checks" section, invoked wiki_edit_guard.py (which DID
        # mutate the target project's real wiki/orca-context-wiki.json:
        # appended the page, bumped content_version), and only THEN
        # failed on the knowledge-body write itself. That left the wiki
        # file modified-and-uncommitted, the candidate stuck at
        # pending_approval forever, and a retry blocked outright by
        # duplicate_page_id -- independently reproduced end-to-end
        # against the pre-fix code before writing this fix. This test
        # asserts the fixed outcome: the wiki file stays untouched, the
        # candidate is never "stuck" (a retry is always possible), and
        # once the permission problem is fixed out-of-band, the retry
        # actually succeeds.
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        knowledge_dir = proj / "wiki" / "knowledge"
        knowledge_dir.mkdir(parents=True)
        os.chmod(str(knowledge_dir), 0o500)  # read+execute, NOT writable
        self.addCleanup(lambda: os.chmod(str(knowledge_dir), 0o700) if knowledge_dir.exists() else None)

        wiki_path = proj / "wiki" / "orca-context-wiki.json"
        before_bytes = wiki_path.read_bytes()
        before_hash = pc._sha256_hex(before_bytes)
        status_before = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(status_before.strip(), "", "fixture project must start with a clean working tree")

        try:
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        finally:
            os.chmod(str(knowledge_dir), 0o700)

        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "target_write_permission_denied")

        # THE critical assertion: the wiki file is byte-for-byte
        # untouched -- NOT mutated-and-uncommitted the way the pre-fix
        # code left it (bumped content_version, appended page).
        after_bytes = wiki_path.read_bytes()
        self.assertEqual(before_bytes, after_bytes, "wiki file must be byte-for-byte identical, never partially mutated")
        self.assertEqual(before_hash, pc._sha256_hex(after_bytes))
        after_doc = json.loads(after_bytes.decode("utf-8"))
        self.assertEqual(after_doc["meta"]["content_version"], 1, "content_version must not have been bumped")
        self.assertEqual(after_doc["pages"], [], "no page must have been appended")

        status_after = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(status_after.strip(), "", "target project's git tree must be clean after a failed approve, never modified-and-uncommitted")

        # The candidate must NOT be stuck: still pending_approval, with no
        # approve_result recorded -- never the old "wiki mutated but
        # candidate never marked approved, retry blocked by
        # duplicate_page_id forever" shape.
        record_path = pc.candidate_path(pc.PROMOTION_ROOT, "proj-a", candidate_id)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "pending_approval")
        self.assertNotIn("approve_result", record)

        # Prove the retry claim for real: fix the permission and approve
        # again -- must now succeed cleanly, with no duplicate_page_id
        # obstruction and no leftover corrupt state from the failed
        # attempt.
        os.chmod(str(knowledge_dir), 0o700)
        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code2, 0, result2)
        self.assertEqual(result2["new_content_version"], 2)
        final_doc = json.loads(wiki_path.read_text(encoding="utf-8"))
        self.assertEqual(len(final_doc["pages"]), 1)
        self.assertEqual(final_doc["pages"][0]["id"], "x-algo-notes")

    def test_approve_injected_oserror_during_knowledge_write_leaves_wiki_untouched(self) -> None:
        # round-4-fix regression, generalized: the fix's guarantee must
        # hold for ANY OSError from the knowledge-body write, not just
        # the named PermissionError case exercised just above (a real
        # disk-full condition, for instance, surfaces as a plain
        # OSError/ENOSPC that atomic_write_in_dir does not convert to a
        # named PromoteFatal -- see its own docstring, which documents
        # only the PermissionError conversion). Inject a disk-full-shaped
        # OSError directly at the one write call that remains BEFORE the
        # tracked wiki write after the reorder, and confirm the wiki file
        # is still never touched and the candidate is never left stuck.
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        wiki_path = proj / "wiki" / "orca-context-wiki.json"
        before_bytes = wiki_path.read_bytes()

        real_atomic_write_in_dir = pc.atomic_write_in_dir

        def failing_atomic_write_in_dir(final_path, payload, *, dir_fd=None):
            if dir_fd is not None:
                # Simulated disk-full: a plain OSError, deliberately NOT
                # a PermissionError, to prove the fix's ordering
                # guarantee does not depend on atomic_write_in_dir's own
                # PermissionError-to-PromoteFatal conversion.
                raise OSError(28, "No space left on device")
            return real_atomic_write_in_dir(final_path, payload, dir_fd=dir_fd)

        with mock.patch.object(pc, "atomic_write_in_dir", side_effect=failing_atomic_write_in_dir):
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )

        self.assertEqual(code, 4, result)

        after_bytes = wiki_path.read_bytes()
        self.assertEqual(before_bytes, after_bytes, "wiki file must be byte-for-byte identical after an injected write failure")
        after_doc = json.loads(after_bytes.decode("utf-8"))
        self.assertEqual(after_doc["meta"]["content_version"], 1)
        self.assertEqual(after_doc["pages"], [])

        status_after = subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(status_after.strip(), "", "target project's git tree must be clean after a failed approve")

        record_path = pc.candidate_path(pc.PROMOTION_ROOT, "proj-a", candidate_id)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "pending_approval")
        self.assertNotIn("approve_result", record)

        # Retry (with the injected failure removed) must succeed cleanly
        # -- proving the candidate was never actually stuck.
        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code2, 0, result2)

    def test_approve_wiki_file_symlink_swap_before_guard_invocation_refused(self) -> None:
        # round-5-fix P1 regression, scenario 1 (the exact repro): with
        # round-4's fix in place, invoke_wiki_edit_guard() -- which passes
        # str(wiki_path) to wiki_edit_guard.py as a subprocess argument,
        # and that file's own main() does its own
        # `.expanduser().resolve(strict=False)` on that string -- is now
        # unavoidably the LAST mutating step in _approve_orca_context_wiki().
        # An attacker with write access to the target project's wiki/
        # directory can swap wiki/orca-context-wiki.json itself for a
        # symlink to a file OUTSIDE the project in the window between the
        # earlier identity_unchanged() check (which now runs before the
        # knowledge-body write) and invoke_wiki_edit_guard(). Reproduced
        # here by hooking atomic_write_in_dir (the knowledge-body write,
        # the last thing that runs before this fix's new re-checks) to
        # perform the swap immediately after it completes -- i.e. as late
        # as possible before invoke_wiki_edit_guard() would otherwise run.
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        wiki_path = proj / "wiki" / "orca-context-wiki.json"
        before_bytes = wiki_path.read_bytes()

        outside_dir = Path(tempfile.mkdtemp(prefix="promote-cap-outside-file-"))
        self.addCleanup(shutil.rmtree, str(outside_dir), ignore_errors=True)
        outside_file = outside_dir / "decoy-wiki.json"
        outside_file.write_bytes(before_bytes)

        git_head_before = subprocess.run(
            ["git", "-C", str(proj), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()

        real_atomic_write_in_dir = pc.atomic_write_in_dir

        def swap_then_write(final_path, payload, *, dir_fd=None):
            result = real_atomic_write_in_dir(final_path, payload, dir_fd=dir_fd)
            # Attacker swaps the real wiki file for a symlink to a file
            # OUTSIDE the project, right in the window this fix's late
            # re-check exists to close.
            wiki_path.unlink()
            wiki_path.symlink_to(outside_file)
            return result

        with mock.patch.object(pc, "atomic_write_in_dir", side_effect=swap_then_write):
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )

        # approve must NOT report success.
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "wiki_path_symlink_introduced")

        # The OUTSIDE file must be completely untouched -- no new page, no
        # bumped content_version, byte-identical to what it was before.
        self.assertEqual(outside_file.read_bytes(), before_bytes, "the outside file must never be mutated")

        # The candidate must NOT have transitioned to approved.
        record_path = pc.candidate_path(pc.PROMOTION_ROOT, "proj-a", candidate_id)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "pending_approval")
        self.assertNotIn("approve_result", record)

        # No git commit happened in the target project.
        git_head_after = subprocess.run(
            ["git", "-C", str(proj), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(git_head_before, git_head_after, "no commit must have happened against the fake project")

    def test_approve_wiki_dir_symlink_swap_before_guard_invocation_refused(self) -> None:
        # round-5-fix P1 regression, scenario 2 (the directory-swap
        # variant): wiki/ ITSELF replaced with a symlink to an outside
        # directory holding a decoy copy of the wiki file, in the same
        # window as the scenario-1 test above. Before this fix,
        # invoke_wiki_edit_guard() would follow the swapped-in wiki/ down
        # to the decoy file (git then correctly refuses the
        # symlink-crossing pathspec, so git_committed would be False, but
        # the decoy copy would have been mutated and approve would still
        # report status "approved").
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        wiki_dir = proj / "wiki"
        wiki_path = wiki_dir / "orca-context-wiki.json"
        before_bytes = wiki_path.read_bytes()

        outside_dir = Path(tempfile.mkdtemp(prefix="promote-cap-outside-dir-"))
        self.addCleanup(shutil.rmtree, str(outside_dir), ignore_errors=True)
        outside_wiki_copy = outside_dir / "orca-context-wiki.json"
        outside_wiki_copy.write_bytes(before_bytes)

        git_head_before = subprocess.run(
            ["git", "-C", str(proj), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()

        real_atomic_write_in_dir = pc.atomic_write_in_dir

        def swap_then_write(final_path, payload, *, dir_fd=None):
            result = real_atomic_write_in_dir(final_path, payload, dir_fd=dir_fd)
            # Attacker replaces wiki/ ITSELF with a symlink to an outside
            # directory holding a decoy copy of the wiki file.
            shutil.rmtree(str(wiki_dir))
            wiki_dir.symlink_to(outside_dir, target_is_directory=True)
            return result

        with mock.patch.object(pc, "atomic_write_in_dir", side_effect=swap_then_write):
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )

        # approve must NOT report success.
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "wiki_path_symlink_introduced")

        # The OUTSIDE decoy copy must be completely untouched.
        self.assertEqual(outside_wiki_copy.read_bytes(), before_bytes, "the outside decoy copy must never be mutated")

        # The candidate must NOT have transitioned to approved.
        record_path = pc.candidate_path(pc.PROMOTION_ROOT, "proj-a", candidate_id)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "pending_approval")
        self.assertNotIn("approve_result", record)

        # No git commit happened in the target project. (wiki/ no longer
        # exists as a real directory under proj at all at this point --
        # it was replaced by a symlink -- so `git -C proj` still works
        # fine since .git itself was never touched.)
        git_head_after = subprocess.run(
            ["git", "-C", str(proj), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(git_head_before, git_head_after, "no commit must have happened against the fake project")

    def test_wiki_dir_fd_symlink_swap_after_recheck_refused_or_safe(self) -> None:
        # round-6-fix verification: the two tests immediately above
        # reproduce a swap injected via atomic_write_in_dir (the
        # knowledge-body write), which runs BEFORE the round-5 re-checks
        # -- those re-checks already catch a swap injected that early, on
        # both round-5 code and this round's. The TRUE round-5 residual
        # (what a dedicated review actually measured and won 10/20 and
        # 20/20) is the window AFTER those re-checks pass and
        # _open_wiki_dir_fd_for_guard() has already opened its dir_fd, but
        # BEFORE the guard subprocess actually touches the filesystem --
        # dominated by that subprocess's own startup latency. This test
        # injects the swap at exactly that point: hooking
        # pc.subprocess.run (invoke_wiki_edit_guard()'s own call, the
        # first subprocess.run call whose argv names wiki_edit_guard.py)
        # to perform the swap immediately before delegating to the real
        # subprocess.run -- i.e. as late as this process can possibly act,
        # right before the real work the residual concerns actually
        # begins in a genuine concurrent attacker.
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        candidate_id = self._draft_and_get_id("proj-a")

        wiki_dir = proj / "wiki"
        wiki_path = wiki_dir / "orca-context-wiki.json"
        before_bytes = wiki_path.read_bytes()

        outside_dir = Path(tempfile.mkdtemp(prefix="promote-cap-outside-dirfd-"))
        self.addCleanup(shutil.rmtree, str(outside_dir), ignore_errors=True)
        outside_wiki_copy = outside_dir / "orca-context-wiki.json"
        outside_wiki_copy.write_bytes(before_bytes)

        moved_aside = proj / "wiki-real"
        real_subprocess_run = pc.subprocess.run
        swap_state = {"done": False}

        def swap_then_run(argv, *args, **kwargs):
            if not swap_state["done"] and any("wiki_edit_guard.py" in str(a) for a in argv):
                swap_state["done"] = True
                # THE attack: by now, _open_wiki_dir_fd_for_guard() has
                # ALREADY opened a directory fd anchored to wiki/'s
                # current inode (this is the fd this very subprocess.run
                # call is about to pass into the child via pass_fds).
                # Swap "wiki" itself for a symlink to an OUTSIDE
                # directory holding a decoy copy, as late as possible
                # before the child actually runs.
                os.rename(str(wiki_dir), str(moved_aside))
                wiki_dir.symlink_to(outside_dir, target_is_directory=True)
            return real_subprocess_run(argv, *args, **kwargs)

        with mock.patch.object(pc.subprocess, "run", side_effect=swap_then_run):
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )

        # THE critical assertion, unconditionally: the outside decoy must
        # NEVER be mutated, no matter what approve reports.
        self.assertEqual(outside_wiki_copy.read_bytes(), before_bytes, "the outside decoy copy must never be mutated")

        # approve must not silently pretend nothing happened, but it also
        # must not report a false story. Since the dir_fd was already
        # pinned to the ORIGINAL wiki/ directory's inode before the swap,
        # the guard's own read/write (through that fd, by basename only)
        # is completely unaffected by the later rename+symlink of the
        # NAME "wiki" -- so the write itself succeeds, correctly, in the
        # ORIGINAL directory (now reachable at its post-swap name).
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["new_content_version"], 2)

        real_wiki_path = moved_aside / "orca-context-wiki.json"
        self.assertTrue(real_wiki_path.is_file(), "the real write must have landed in the ORIGINALLY-opened directory")
        real_doc = json.loads(real_wiki_path.read_bytes().decode("utf-8"))
        self.assertEqual(real_doc["meta"]["content_version"], 2)
        self.assertEqual(len(real_doc["pages"]), 1)
        self.assertEqual(real_doc["pages"][0]["id"], "x-algo-notes")

        real_knowledge_md = moved_aside / "knowledge" / "x-algo-notes.md"
        self.assertTrue(real_knowledge_md.is_file())
        self.assertIn("正文内容", real_knowledge_md.read_text(encoding="utf-8"))

        # The LATER git-commit step operates on "wiki/orca-context-wiki.json"
        # by NAME (git has no dir_fd-anchoring concept), and "wiki" is now
        # a symlink pointing OUTSIDE the repository -- git itself refuses
        # a pathspec that crosses a symlink out of the worktree ("fatal:
        # pathspec '...' is beyond a symbolic link"), so the commit
        # candidly fails. This is exactly the module docstring's own
        # documented, already-accepted limitation ("if the wiki write
        # succeeds but the subsequent git commit fails ... write is NOT
        # rolled back ... approve reports this candidly") -- not a new
        # failure mode this fix introduces, and never a false claim that
        # the outside decoy was what got committed.
        self.assertFalse(result["git_committed"])
        self.assertIsNone(result["git_commit_sha"])
        self.assertIsNotNone(result.get("git_commit_error"))

        # The candidate DID transition to approved (the write really did
        # succeed, safely, inside the project) -- record reflects that
        # honestly, consistent with the CLI's own reported result.
        record_path = pc.candidate_path(pc.PROMOTION_ROOT, "proj-a", candidate_id)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "approved")


class KnowledgeDirFdTocTouTests(unittest.TestCase):
    """round-4-fix regression: a dedicated Grok final gate demonstrated
    that the ORIGINAL caller of resolve_knowledge_md_path() took the
    plain Path it returned and re-walked it BY NAME much later, after a
    real subprocess call (invoke_wiki_edit_guard()) had already run in
    between -- a genuine TOCTOU window. An attacker with write access to
    wiki/ could swap wiki/knowledge for a symlink to outside the target
    project during that window; the pre-fix code would then silently
    follow the new symlink and write the knowledge body outside the
    project entirely, while approve still reported success.

    These tests exercise the fix -- the directory file descriptor
    _resolve_knowledge_md_path_and_dir_fd() hands back -- directly and
    deterministically: swap the directory for a symlink AFTER
    containment was confirmed but BEFORE the write, and confirm the
    write stays anchored to the ORIGINAL directory's inode, never the
    new symlink target."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="knowledge-dir-fd-toctou-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.proj = self.tmp / "proj"
        (self.proj / "wiki").mkdir(parents=True)

    def test_symlink_swap_after_containment_check_does_not_escape(self) -> None:
        knowledge_final, dir_fd = pc._resolve_knowledge_md_path_and_dir_fd(self.proj, "a-page")
        knowledge_dir = self.proj / "wiki" / "knowledge"
        self.assertTrue(knowledge_dir.is_dir())

        # THE attack: an attacker with write access to wiki/ swaps
        # wiki/knowledge for a symlink to outside the project, in the
        # window between containment-check (just above) and the actual
        # write (just below) -- exactly the window a dedicated Grok final
        # gate demonstrated was exploitable against the pre-fix code. The
        # real directory has to move somewhere before a symlink can take
        # its name; renaming it (rather than deleting it) also lets this
        # test verify exactly where the write actually landed afterward.
        outside = self.tmp / "OUTSIDE"
        outside.mkdir()
        moved_aside = self.proj / "wiki" / "knowledge-real"
        os.rename(str(knowledge_dir), str(moved_aside))
        os.symlink(str(outside), str(knowledge_dir))

        try:
            pc.atomic_write_in_dir(knowledge_final, b"PWNED-CONTENT", dir_fd=dir_fd)
        finally:
            os.close(dir_fd)

        # THE critical assertion: nothing landed at the attacker's
        # symlink target -- the write did not escape the project.
        self.assertEqual(list(outside.iterdir()), [], "no file must land in the swapped-in symlink target")

        # The write landed in the ORIGINAL directory instead -- reachable
        # here at its post-rename name, proving the dir_fd stayed
        # anchored to the checked inode regardless of the by-name swap.
        real_written = moved_aside / "a-page.md"
        self.assertTrue(real_written.is_file(), "the write must have landed in the ORIGINALLY-checked directory")
        self.assertEqual(real_written.read_bytes(), b"PWNED-CONTENT")

        # And the symlinked "knowledge" name itself was never walked by
        # the write -- it still points exactly where the attacker put it.
        self.assertEqual(os.readlink(str(knowledge_dir)), str(outside))

    def test_preexisting_symlink_at_knowledge_dir_refused_outright(self) -> None:
        # A different variant: wiki/knowledge is ALREADY a symlink before
        # containment checking even starts (pre-existing protection, not
        # new in this round -- confirms the refactor into
        # _resolve_knowledge_md_path_and_dir_fd() did not weaken it).
        outside = self.tmp / "OUTSIDE2"
        outside.mkdir()
        os.symlink(str(outside), str(self.proj / "wiki" / "knowledge"))
        with self.assertRaises(pc.PromoteFatal) as ctx:
            pc._resolve_knowledge_md_path_and_dir_fd(self.proj, "a-page")
        self.assertEqual(ctx.exception.reason, "knowledge_dir_is_symlink")
        self.assertEqual(list(outside.iterdir()), [])


# ---------------------------------------------------------------------------
# Approve-time record re-validation (P0 regression): a candidate record file
# living under PROMOTION_ROOT is JSON on disk that approve reads back
# between draft and approve time. An earlier revision trusted every field
# in it completely once draft had validated it once; a dual review
# demonstrated that a candidate record tampered (or corrupted) on disk
# between draft and approve -- specifically its "id" field, which
# resolve_knowledge_md_path() uses directly to build a filesystem path --
# let approve write a file OUTSIDE the target project root entirely, with
# approve reporting status: "approved" and no error. These tests attack
# that exact path end-to-end via the public CLI (draft -> tamper the
# on-disk record -> approve), not via any internal shortcut, and assert on
# the one signal that actually matters: no file landed outside the project
# root.
# ---------------------------------------------------------------------------


class ApproveTimeRecordTamperingTests(PromoteCapabilityTestCase):
    def _traversal_id_to(self, proj: Path, outside_target: Path) -> str:
        """Build a page_id whose "/"-joined segments walk from
        <proj>/wiki/knowledge/ to outside_target, purely lexically (the
        directories involved need not exist yet)."""
        knowledge_dir = proj / "wiki" / "knowledge"
        rel = os.path.relpath(str(outside_target), start=str(knowledge_dir))
        return rel.replace(os.sep, "/")

    def test_approve_refuses_tampered_id_path_traversal_knowledge(self) -> None:
        proj = self.make_project("declared-project")
        self.write_catalog({"declared-project": proj})
        code, result, _err = self.draft_knowledge("declared-project", id="legit-page")
        self.assertEqual(code, 0, result)
        candidate_id = result["candidate_id"]

        # A directory that is a SIBLING of self.tmp's "projects" dir --
        # unambiguously outside declared-project's own root, and outside
        # every other fake project this test file could ever create too.
        outside_target = self.tmp / "OUTSIDE_TARGET"
        outside_target.mkdir(parents=True, exist_ok=True)
        pwned_file = outside_target / "PWNED-file.md"

        candidate_file = self.promotion_root / "declared-project" / f"{candidate_id}.json"
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        traversal_id = self._traversal_id_to(proj, outside_target)
        self.assertIn("/", traversal_id, "the payload must actually contain a path separator to exercise the bug")
        record["id"] = traversal_id
        candidate_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        # THE critical assertion: no file was written outside the project
        # root, regardless of which exit code/reason approve settles on.
        self.assertFalse(
            pwned_file.exists(),
            f"path-traversal write escaped the project root: {pwned_file} must not exist",
        )
        self.assertNotEqual(code2, 0, result2)
        self.assertEqual(result2.get("reason"), "bad_id", result2)

        # The candidate must not have been silently marked approved either.
        after_record = json.loads(candidate_file.read_text(encoding="utf-8"))
        self.assertEqual(after_record["status"], "pending_approval")

    def test_approve_refuses_tampered_id_path_traversal_capability(self) -> None:
        # Even though the "reusable-capabilities.json" branch never uses
        # "id" to build a filesystem path directly (see
        # _approve_reusable_capabilities), the SAME tampered-record threat
        # model applies to it, and the fix (re-validate the whole record,
        # not just knowledge candidates) must reject it too, on the same
        # ID_RE grammar grounds, before it ever reaches the target file.
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_capability("proj-a", id="my-cap")
        self.assertEqual(code, 0, result)
        candidate_id = result["candidate_id"]

        candidate_file = self.promotion_root / "proj-a" / f"{candidate_id}.json"
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        record["id"] = "../../../etc/PWNED"
        candidate_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        before = (proj / "wiki" / "reusable-capabilities.json").read_bytes()
        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertNotEqual(code2, 0, result2)
        self.assertEqual(result2.get("reason"), "bad_id", result2)
        after = (proj / "wiki" / "reusable-capabilities.json").read_bytes()
        self.assertEqual(before, after, "no write should happen once the tampered id fails re-validation")

    def test_approve_refuses_tampered_path_field(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_capability("proj-a")
        self.assertEqual(code, 0, result)
        candidate_id = result["candidate_id"]

        candidate_file = self.promotion_root / "proj-a" / f"{candidate_id}.json"
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        record["path"] = "../../../../etc/passwd"
        candidate_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertNotEqual(code2, 0, result2)
        self.assertEqual(result2.get("reason"), "bad_field", result2)

    def test_approve_refuses_tampered_operation(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_capability("proj-a")
        candidate_id = result["candidate_id"]
        candidate_file = self.promotion_root / "proj-a" / f"{candidate_id}.json"
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        record["operation"] = "bogus_operation"
        candidate_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertNotEqual(code2, 0, result2)
        self.assertEqual(result2.get("reason"), "bad_operation", result2)

    def test_approve_still_succeeds_for_an_untampered_record(self) -> None:
        # Sanity/non-regression: the new re-validation step must not reject
        # a perfectly normal candidate that was never tampered with.
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_capability("proj-a")
        candidate_id = result["candidate_id"]
        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code2, 0, result2)


class ApproveTargetProjectAndCandidateIdIntegrityTests(PromoteCapabilityTestCase):
    """round-2-fix round-2 (post-NO-GO): a fully independent tamper vector
    from ApproveTimeRecordTamperingTests above. "target_project" and
    "candidate_id" both pass _revalidate_record_for_approve()'s format
    checks trivially -- a real, catalog-known OTHER project name, or a
    syntactically valid uuid-like string, is not itself malformed. The bug
    is that save_candidate()/content_md_path() rebuild a path FROM these
    record fields rather than reusing the path find_candidate() actually
    found the record at. See _verify_record_matches_found_location()'s own
    docstring for the full exploit description."""

    def test_approve_refuses_target_project_redirected_to_another_catalog_project(self) -> None:
        victim = self.make_project("victim")
        attacker_owned = self.make_project("attacker-owned")
        self.write_catalog({"victim": victim, "attacker-owned": attacker_owned})
        code, result, _err = self.draft_capability("victim", id="my-cap")
        self.assertEqual(code, 0, result)
        candidate_id = result["candidate_id"]

        candidate_file = self.promotion_root / "victim" / f"{candidate_id}.json"
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        record["target_project"] = "attacker-owned"
        candidate_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        victim_before = (victim / "wiki" / "reusable-capabilities.json").read_bytes()
        attacker_before = (attacker_owned / "wiki" / "reusable-capabilities.json").read_bytes()

        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertNotEqual(code2, 0, result2)
        self.assertEqual(result2.get("reason"), "record_location_mismatch", result2)

        # Neither project's real file was touched.
        self.assertEqual(victim_before, (victim / "wiki" / "reusable-capabilities.json").read_bytes())
        self.assertEqual(attacker_before, (attacker_owned / "wiki" / "reusable-capabilities.json").read_bytes())

        # No split record: nothing was created under attacker-owned's
        # PROMOTION_ROOT directory, and the original file still says
        # pending_approval.
        self.assertFalse((self.promotion_root / "attacker-owned").exists())
        after_record = json.loads(candidate_file.read_text(encoding="utf-8"))
        self.assertEqual(after_record["status"], "pending_approval")

    def test_reject_refuses_target_project_redirected_to_another_catalog_project(self) -> None:
        # Same tamper, exercised through _terminal_transition (reject)
        # rather than approve -- reject/withdraw never touch a real target
        # project file, but they DO call save_candidate(), which has the
        # exact same rebuild-from-record-fields bug.
        victim = self.make_project("victim")
        attacker_owned = self.make_project("attacker-owned")
        self.write_catalog({"victim": victim, "attacker-owned": attacker_owned})
        code, result, _err = self.draft_capability("victim", id="my-cap")
        self.assertEqual(code, 0, result)
        candidate_id = result["candidate_id"]

        candidate_file = self.promotion_root / "victim" / f"{candidate_id}.json"
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        record["target_project"] = "attacker-owned"
        candidate_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        code2, result2, _err2 = run_cli_json(
            ["reject", "--candidate-id", candidate_id, "--decided-by", "t", "--rationale", "r"]
        )
        self.assertNotEqual(code2, 0, result2)
        self.assertEqual(result2.get("reason"), "record_location_mismatch", result2)
        self.assertFalse((self.promotion_root / "attacker-owned").exists())
        after_record = json.loads(candidate_file.read_text(encoding="utf-8"))
        self.assertEqual(after_record["status"], "pending_approval")

    def test_approve_refuses_candidate_id_field_mismatch_before_any_wiki_write(self) -> None:
        # The dangerous variant: the FILE is found correctly (via the real
        # --candidate-id CLI arg matching the real filename), but the
        # record's internal "candidate_id" field -- used later by
        # content_md_path() to locate the staged content.md -- has been
        # changed to point somewhere else. Before this round's fix, this
        # was only discovered AFTER _approve_orca_context_wiki() had
        # already written the new page + bumped content_version into the
        # target project's real wiki file (a partial write with no
        # matching "approved" candidate record).
        proj = self.make_project("proj-a", wiki_content_version=1)
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_knowledge("proj-a")
        self.assertEqual(code, 0, result)
        real_candidate_id = result["candidate_id"]

        candidate_file = self.promotion_root / "proj-a" / f"{real_candidate_id}.json"
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        self.assertTrue(record["has_content_md"])
        record["candidate_id"] = "cand-" + ("0" * 20)  # syntactically fine, just WRONG
        candidate_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        wiki_path = proj / "wiki" / "orca-context-wiki.json"
        before = wiki_path.read_bytes()

        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", real_candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertNotEqual(code2, 0, result2)
        self.assertEqual(result2.get("reason"), "record_location_mismatch", result2)

        # THE critical assertion: the target project's real wiki file must
        # be byte-for-byte untouched -- no partial write, no bumped
        # content_version, nothing to roll back.
        after = wiki_path.read_bytes()
        self.assertEqual(before, after, "no write should reach the target project's wiki file once the id mismatch is caught")
        after_doc = json.loads(after.decode("utf-8"))
        self.assertEqual(after_doc["meta"]["content_version"], 1)
        self.assertEqual(after_doc["pages"], [])

    def test_approve_still_succeeds_with_untampered_target_project_and_candidate_id(self) -> None:
        # Sanity/non-regression companion to the two attacks above.
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = self.draft_capability("proj-a")
        candidate_id = result["candidate_id"]
        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code2, 0, result2)


class ResolveKnowledgeMdPathContainmentTests(unittest.TestCase):
    """Layer 2 in isolation: resolve_knowledge_md_path()'s own containment
    check over the FINAL page_id-derived path, independent of the approve-
    time re-validation tested above. Calling the function directly (not
    through the CLI) proves this layer holds even if some future caller of
    resolve_knowledge_md_path() ever skipped the upstream re-validation."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="knowledge-path-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.proj = self.tmp / "proj"
        (self.proj / "wiki").mkdir(parents=True)

    def test_traversal_page_id_refused(self) -> None:
        outside = self.tmp / "OUTSIDE"
        outside.mkdir()
        rel = os.path.relpath(str(outside), start=str(self.proj / "wiki" / "knowledge"))
        malicious_id = rel.replace(os.sep, "/") + "/PWNED"
        with self.assertRaises(pc.PromoteFatal) as ctx:
            pc.resolve_knowledge_md_path(self.proj, malicious_id)
        self.assertEqual(ctx.exception.reason, "target_knowledge_path_escapes_knowledge_dir")
        self.assertFalse((outside / "PWNED.md").exists())

    def test_normal_page_id_still_resolves_inside_knowledge_dir(self) -> None:
        result = pc.resolve_knowledge_md_path(self.proj, "a-normal-id")
        expected = (self.proj / "wiki" / "knowledge" / "a-normal-id.md").resolve()
        self.assertEqual(result, expected)

    def test_nested_traversal_with_extra_segments_refused(self) -> None:
        # A payload that dips outside and back in (still ends up escaping
        # overall) must also be refused, not just a pure "../../.." prefix.
        outside = self.tmp / "OUTSIDE2"
        outside.mkdir()
        rel = os.path.relpath(str(outside), start=str(self.proj / "wiki" / "knowledge"))
        malicious_id = rel.replace(os.sep, "/") + "/nested/PWNED"
        with self.assertRaises(pc.PromoteFatal):
            pc.resolve_knowledge_md_path(self.proj, malicious_id)


# ---------------------------------------------------------------------------
# CLI: reject / withdraw
# ---------------------------------------------------------------------------


class RejectWithdrawTests(PromoteCapabilityTestCase):
    def test_reject_transitions_status_without_touching_project_files(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code0, result0, _e0 = self.draft_capability("proj-a")
        candidate_id = result0["candidate_id"]
        before = (proj / "wiki" / "reusable-capabilities.json").read_bytes()

        code, result, _err = run_cli_json(["reject", "--candidate-id", candidate_id, "--decided-by", "t", "--rationale", "not needed"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "rejected")
        after = (proj / "wiki" / "reusable-capabilities.json").read_bytes()
        self.assertEqual(before, after)

        record = json.loads((self.promotion_root / "proj-a" / f"{candidate_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["decided_by"], "t")

    def test_withdraw_transitions_status(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        _c, result0, _e = self.draft_capability("proj-a")
        code, result, _err = run_cli_json(["withdraw", "--candidate-id", result0["candidate_id"], "--decided-by", "t", "--rationale", "changed my mind"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "withdrawn")

    def test_reject_already_decided_candidate_fails(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        _c, result0, _e = self.draft_capability("proj-a")
        candidate_id = result0["candidate_id"]
        run_cli_json(["withdraw", "--candidate-id", candidate_id, "--decided-by", "t", "--rationale", "r"])
        code, result, _err = run_cli_json(["reject", "--candidate-id", candidate_id, "--decided-by", "t", "--rationale", "r"])
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "candidate_not_pending")

    def test_reject_missing_rationale_is_usage_error(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        _c, result0, _e = self.draft_capability("proj-a")
        code, result, _err = run_cli_json(["reject", "--candidate-id", result0["candidate_id"], "--decided-by", "t", "--rationale", "  "])
        self.assertEqual(code, 2)

    def test_reject_unknown_candidate(self) -> None:
        code, result, _err = run_cli_json(["reject", "--candidate-id", "cand-nope", "--decided-by", "t", "--rationale", "r"])
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "candidate_not_found")


# ---------------------------------------------------------------------------
# CLI: amend
# ---------------------------------------------------------------------------


class AmendTests(PromoteCapabilityTestCase):
    def _seed_published_capability(self, proj: Path, *, kind: str = "script", name: str = "base.py", depends_on=None) -> None:
        doc_path = proj / "wiki" / "reusable-capabilities.json"
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        doc["capabilities"].append(
            {
                "id": "base-cap",
                "kind": kind,
                "name": name,
                "path": "scripts/my_script.py",
                "summary": "the base capability",
                "last_verified_at": None,
                "depends_on": depends_on or [],
            }
        )
        # Same-project depends_on refs must resolve WITHIN the file
        # (validate_document()'s dangling_local_ref rule, exercised for
        # real by approve's post-write self-check) -- seed the entry that
        # "script:other.py" amends will point at so a real amend test
        # exercises a genuinely valid end state, not a self-inflicted
        # dangling reference.
        doc["capabilities"].append(
            {
                "id": "other-cap",
                "kind": "script",
                "name": "other.py",
                "path": "scripts/my_script.py",
                "summary": "the capability amends point at",
                "last_verified_at": None,
                "depends_on": [],
            }
        )
        doc_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _run_git(["add", "-A"], proj)
        _run_git(["commit", "-q", "-m", "seed base capability"], proj)

    def test_amend_draft_then_approve_appends_depends_on(self) -> None:
        proj = self.make_project("proj-a")
        self._seed_published_capability(proj)
        self.write_catalog({"proj-a": proj}, capability_ref_index={"proj-a:script:base.py": "proj-a#base-cap"})

        code, result, _err = run_cli_json(
            [
                "amend",
                "--target-project",
                "proj-a",
                "--target",
                "script:base.py",
                "--add-depends-on",
                "script:other.py",
                "--source",
                "human",
                "--catalog",
                str(self.catalog_path),
            ]
        )
        self.assertEqual(code, 0, result)
        self.assertTrue(result["amend_target_known_in_catalog"])
        candidate_id = result["candidate_id"]

        code2, result2, _e2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code2, 0, result2)
        doc = json.loads((proj / "wiki" / "reusable-capabilities.json").read_text(encoding="utf-8"))
        self.assertIn("script:other.py", doc["capabilities"][0]["depends_on"])

    def test_amend_target_not_found_at_approve_time(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, result, _err = run_cli_json(
            [
                "amend",
                "--target-project",
                "proj-a",
                "--target",
                "script:does-not-exist.py",
                "--add-depends-on",
                "script:other.py",
                "--source",
                "human",
                "--catalog",
                str(self.catalog_path),
            ]
        )
        self.assertEqual(code, 0, result)
        code2, result2, _e2 = run_cli_json(
            ["approve", "--candidate-id", result["candidate_id"], "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code2, 1)
        self.assertEqual(result2["reason"], "amend_target_not_found")

    def test_amend_depends_on_already_present_rejected(self) -> None:
        proj = self.make_project("proj-a")
        self._seed_published_capability(proj, depends_on=["script:other.py"])
        self.write_catalog({"proj-a": proj})
        code, result, _err = run_cli_json(
            [
                "amend",
                "--target-project",
                "proj-a",
                "--target",
                "script:base.py",
                "--add-depends-on",
                "script:other.py",
                "--source",
                "human",
                "--catalog",
                str(self.catalog_path),
            ]
        )
        self.assertEqual(code, 0, result)
        code2, result2, _e2 = run_cli_json(
            ["approve", "--candidate-id", result["candidate_id"], "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code2, 1)
        self.assertEqual(result2["reason"], "depends_on_already_present")

    def test_amend_self_reference_rejected(self) -> None:
        self.write_catalog({})
        code, result, _err = run_cli_json(
            [
                "amend",
                "--target-project",
                "proj-a",
                "--target",
                "script:base.py",
                "--add-depends-on",
                "script:base.py",
                "--source",
                "human",
                "--catalog",
                str(self.catalog_path),
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["reason"], "self_reference")

    def test_amend_bad_target_grammar_usage_error(self) -> None:
        self.write_catalog({})
        code, result, _err = run_cli_json(
            [
                "amend",
                "--target-project",
                "proj-a",
                "--target",
                "not-valid-grammar",
                "--add-depends-on",
                "script:other.py",
                "--source",
                "human",
                "--catalog",
                str(self.catalog_path),
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["reason"], "bad_target")

    def test_amend_free_text_source_not_coupled_to_m8_1(self) -> None:
        # --source is deliberately free text -- any non-empty string works,
        # including a shape that only means something once a future signal
        # (M8-1) is independently authorized.
        self.write_catalog({})
        code, result, _err = run_cli_json(
            [
                "amend",
                "--target-project",
                "proj-a",
                "--target",
                "script:base.py",
                "--add-depends-on",
                "script:other.py",
                "--source",
                "mention-evidence:edge-1234",
                "--catalog",
                str(self.catalog_path),
            ]
        )
        self.assertEqual(code, 0, result)

    def test_amend_approve_refuses_proposed_target_retargeted_to_wiki(self) -> None:
        # round-2-fix round-2 (post-NO-GO): cmd_amend() always hardcodes
        # proposed_target to "reusable-capabilities.json" -- it is never
        # legitimately "orca-context-wiki.json" for an amend_depends_on
        # candidate. A record whose on-disk proposed_target is retargeted
        # to "orca-context-wiki.json" while operation stays
        # "amend_depends_on" must never reach _approve_orca_context_wiki(),
        # which would otherwise write a page of literal nulls
        # (id/title/path/status are all None on an amend record -- see
        # cmd_amend) into the target project's real wiki file and commit
        # it, reporting "approved" success.
        proj = self.make_project("proj-a", wiki_content_version=1)
        self._seed_published_capability(proj)
        self.write_catalog({"proj-a": proj})
        code, result, _err = run_cli_json(
            [
                "amend",
                "--target-project",
                "proj-a",
                "--target",
                "script:base.py",
                "--add-depends-on",
                "script:other.py",
                "--source",
                "human",
                "--catalog",
                str(self.catalog_path),
            ]
        )
        self.assertEqual(code, 0, result)
        candidate_id = result["candidate_id"]

        candidate_file = self.promotion_root / "proj-a" / f"{candidate_id}.json"
        record = json.loads(candidate_file.read_text(encoding="utf-8"))
        self.assertEqual(record["operation"], "amend_depends_on")
        record["proposed_target"] = "orca-context-wiki.json"
        candidate_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        wiki_path = proj / "wiki" / "orca-context-wiki.json"
        before = wiki_path.read_bytes()
        caps_before = (proj / "wiki" / "reusable-capabilities.json").read_bytes()

        code2, result2, _err2 = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        self.assertNotEqual(code2, 0, result2)
        self.assertEqual(result2.get("reason"), "bad_proposed_target_for_operation", result2)

        after = wiki_path.read_bytes()
        self.assertEqual(before, after, "no null-field page may be written to the target wiki")
        after_doc = json.loads(after.decode("utf-8"))
        self.assertEqual(after_doc["meta"]["content_version"], 1)
        self.assertEqual(after_doc["pages"], [])
        self.assertEqual(caps_before, (proj / "wiki" / "reusable-capabilities.json").read_bytes())

        log = subprocess.run(
            ["git", "-C", str(proj), "log", "--oneline"], capture_output=True, text=True, check=True
        ).stdout
        self.assertNotIn("promote_capability: approve", log)


# ---------------------------------------------------------------------------
# Concurrency-lock regression tests.
#
# An independent review demonstrated two real, reproducible TOCTOU races in
# an earlier revision of this file:
#   1. cmd_approve acquired NO lock at all. Two concurrent `approve` calls
#      against different candidates targeting the SAME project's SAME wiki
#      file could each pass their own fstat-identity recheck (since neither
#      had written yet) and then race to write -- whichever wrote second
#      silently discarded the first call's already-"approved" write, with
#      no error and no trace.
#   2. cmd_draft/cmd_amend computed their exact-duplicate check BEFORE
#      acquiring the lock that protects their own write, so two concurrent
#      submissions of the byte-identical candidate could both observe "no
#      existing duplicate" and both write a pending_approval record.
#
# Both are fixed by moving the read-check-write sequence inside the same
# PROMOTION_ROOT lock draft/amend already used for their own writes. Each
# test below widens the exact race window the original bug needed by
# inserting a controlled sleep at the precise point the bug's own
# read-then-decide happened -- not by changing any decision logic -- so a
# passing test here is proof the LOCK, not lucky scheduling, prevents the
# race. See module docstring's "THE SINGLE WRITE EXCEPTION" section.
# ---------------------------------------------------------------------------


class ConcurrencyLockTests(PromoteCapabilityTestCase):
    def test_concurrent_approve_does_not_silently_lose_a_write(self) -> None:
        proj = self.make_project("proj-race")
        self.write_catalog({"proj-race": proj})

        code_a, res_a, _ = self.draft_capability("proj-race", id="race-a", name="race_a.py")
        code_b, res_b, _ = self.draft_capability("proj-race", id="race-b", name="race_b.py")
        self.assertEqual(code_a, 0, res_a)
        self.assertEqual(code_b, 0, res_b)
        cand_a, cand_b = res_a["candidate_id"], res_b["candidate_id"]

        real_identity_unchanged = pc.identity_unchanged
        slow = threading.local()

        def patched(path: Path, identity: tuple) -> bool:
            result = real_identity_unchanged(path, identity)
            if getattr(slow, "on", False):
                time.sleep(0.5)
            return result

        pc.identity_unchanged = patched
        self.addCleanup(setattr, pc, "identity_unchanged", real_identity_unchanged)

        outcomes: dict[str, tuple[int, dict, str]] = {}

        def worker(key: str, candidate_id: str, make_slow: bool) -> None:
            slow.on = make_slow
            outcomes[key] = run_cli_json(
                [
                    "approve",
                    "--candidate-id",
                    candidate_id,
                    "--catalog",
                    str(self.catalog_path),
                    "--approved-by",
                    "t",
                    "--rationale",
                    "race",
                ]
            )

        t_a = threading.Thread(target=worker, args=("A", cand_a, True))
        t_b = threading.Thread(target=worker, args=("B", cand_b, False))
        t_a.start()
        time.sleep(0.15)  # let A acquire the lock and enter the slowed identity check first
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        code_a2, res_a2, _ = outcomes["A"]
        code_b2, res_b2, _ = outcomes["B"]

        # Exactly one must succeed (0); the other must fail CLEANLY with a
        # named lock_held reason (4) -- never both exit 0 while one write is
        # silently discarded on disk.
        self.assertEqual(sorted([code_a2, code_b2]), [0, 4], (res_a2, res_b2))
        loser_result = res_a2 if code_a2 == 4 else res_b2
        self.assertEqual(loser_result.get("reason"), "lock_held")

        doc = json.loads((proj / "wiki" / "reusable-capabilities.json").read_text())
        ids_on_disk = sorted(e["id"] for e in doc["capabilities"])
        winner_id = "race-a" if code_a2 == 0 else "race-b"
        # The critical assertion: disk contains EXACTLY the winner's entry.
        # A silent-overwrite bug would show only ONE id here too but paired
        # with the loser's OWN candidate record wrongly claiming "approved"
        # -- checked next.
        self.assertEqual(ids_on_disk, [winner_id])

        loser_candidate_id = cand_b if code_a2 == 0 else cand_a
        found = pc.find_candidate(self.promotion_root, loser_candidate_id)
        self.assertIsNotNone(found)
        self.assertEqual(found[0]["status"], "pending_approval", "the loser must NOT be marked approved when its write never landed")

        # A real caller retries after lock_held; the retry must succeed and
        # must not clobber the winner's already-written entry.
        code_retry, res_retry, _ = run_cli_json(
            [
                "approve",
                "--candidate-id",
                loser_candidate_id,
                "--catalog",
                str(self.catalog_path),
                "--approved-by",
                "t",
                "--rationale",
                "retry-after-lock-held",
            ]
        )
        self.assertEqual(code_retry, 0, res_retry)
        doc2 = json.loads((proj / "wiki" / "reusable-capabilities.json").read_text())
        self.assertEqual(sorted(e["id"] for e in doc2["capabilities"]), ["race-a", "race-b"])

    def test_concurrent_identical_draft_does_not_create_duplicate_pending_record(self) -> None:
        proj = self.make_project("proj-dup")
        self.write_catalog({"proj-dup": proj})

        real_list = pc.list_project_candidates
        slow = threading.local()

        def patched(root: Path, target_project: str) -> list:
            result = real_list(root, target_project)
            if getattr(slow, "on", False):
                time.sleep(0.5)
            return result

        pc.list_project_candidates = patched
        self.addCleanup(setattr, pc, "list_project_candidates", real_list)

        payload = {
            "target_project": "proj-dup",
            "proposed_target": "reusable-capabilities.json",
            "id": "dup-cap",
            "kind": "script",
            "name": "dup_cap.py",
            "path": "scripts/my_script.py",
            "summary": "identical candidate submitted twice concurrently",
            "source": {"mechanism": "human"},
        }
        input_path = self.tmp / "dup-input.json"
        input_path.write_text(json.dumps(payload), encoding="utf-8")

        outcomes: dict[str, tuple[int, dict, str]] = {}

        def worker(key: str, make_slow: bool) -> None:
            slow.on = make_slow
            outcomes[key] = run_cli_json(["draft", "--from-json", str(input_path), "--catalog", str(self.catalog_path)])

        t_a = threading.Thread(target=worker, args=("A", True))
        t_b = threading.Thread(target=worker, args=("B", False))
        t_a.start()
        time.sleep(0.15)
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        code_a, res_a, _ = outcomes["A"]
        code_b, res_b, _ = outcomes["B"]
        self.assertEqual(sorted([code_a, code_b]), [0, 4], (res_a, res_b))
        loser = res_a if code_a == 4 else res_b
        self.assertEqual(loser.get("reason"), "lock_held")

        pending = pc.list_project_candidates(self.promotion_root, "proj-dup")
        self.assertEqual(len(pending), 1, pending)

    def test_concurrent_reject_during_approve_does_not_corrupt_ledger(self) -> None:
        # round-2-fix P1: _terminal_transition() (shared by reject/withdraw)
        # used to run its whole read-check-write sequence with NO lock at
        # all, while draft/amend/approve all serialize under the same
        # PROMOTION_ROOT lock. A dual review demonstrated this let
        # reject/withdraw race a concurrent approve on the SAME pending
        # candidate: on the unfixed code this test reliably (independently
        # reproduced 5/5 runs outside this suite) got BOTH commands to
        # report success, leaving promotion-ledger.jsonl with an "approve"
        # AND a "reject" event for the identical candidate_id -- a
        # self-contradictory audit trail no reader of that ledger could
        # trust. Widen the same way the other tests in this class do: slow
        # down a function approve calls WHILE it holds the lock
        # (load_catalog, called right after acquire_lock in cmd_approve) so
        # a concurrent reject has a real window to attempt to run.
        proj = self.make_project("proj-race2")
        self.write_catalog({"proj-race2": proj})
        code0, result0, _e0 = self.draft_capability("proj-race2", id="race2-cap", name="race2.py")
        self.assertEqual(code0, 0, result0)
        candidate_id = result0["candidate_id"]

        real_load_catalog = pc.load_catalog
        slow = threading.local()

        def patched(path: Path):
            result = real_load_catalog(path)
            if getattr(slow, "on", False):
                time.sleep(0.5)
            return result

        pc.load_catalog = patched
        self.addCleanup(setattr, pc, "load_catalog", real_load_catalog)

        outcomes: dict[str, tuple[int, dict, str]] = {}

        def approve_worker() -> None:
            slow.on = True
            outcomes["approve"] = run_cli_json(
                [
                    "approve",
                    "--candidate-id",
                    candidate_id,
                    "--catalog",
                    str(self.catalog_path),
                    "--approved-by",
                    "t",
                    "--rationale",
                    "race",
                ]
            )

        def reject_worker() -> None:
            slow.on = False
            outcomes["reject"] = run_cli_json(
                ["reject", "--candidate-id", candidate_id, "--decided-by", "t", "--rationale", "concurrent reject attempt"]
            )

        t_approve = threading.Thread(target=approve_worker)
        t_reject = threading.Thread(target=reject_worker)
        t_approve.start()
        time.sleep(0.15)  # let approve acquire the lock and enter the slowed load_catalog()
        t_reject.start()
        t_approve.join(timeout=5)
        t_reject.join(timeout=5)

        code_approve, res_approve, _ = outcomes["approve"]
        code_reject, res_reject, _ = outcomes["reject"]

        # approve must win this race (it started first and got the lock
        # first); reject must fail CLEANLY, never silently succeed while
        # approve is also in flight on the same candidate.
        self.assertEqual(code_approve, 0, res_approve)
        self.assertIn(code_reject, (1, 4), res_reject)
        self.assertIn(res_reject.get("reason"), ("lock_held", "candidate_not_pending"), res_reject)

        ledger_lines = (self.promotion_root / pc.LEDGER_NAME).read_text(encoding="utf-8").strip().splitlines()
        actions_for_candidate = [
            entry["action"] for entry in (json.loads(line) for line in ledger_lines) if entry.get("candidate_id") == candidate_id
        ]
        # THE critical assertion: never both an "approve" and a "reject"
        # ledger entry for the identical candidate_id.
        self.assertNotIn("reject", actions_for_candidate, actions_for_candidate)
        self.assertIn("approve", actions_for_candidate, actions_for_candidate)

        final_record = json.loads((self.promotion_root / "proj-race2" / f"{candidate_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(final_record["status"], "approved")
        doc = json.loads((proj / "wiki" / "reusable-capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual([e["id"] for e in doc["capabilities"]], ["race2-cap"], "the real write must match the final 'approved' status")

    def test_concurrent_withdraw_during_approve_does_not_corrupt_ledger(self) -> None:
        # Same race as above, for withdraw (the other caller of the shared
        # _terminal_transition()) -- both callers share the same fix, but
        # each has its own dedicated test rather than assuming symmetry.
        proj = self.make_project("proj-race3")
        self.write_catalog({"proj-race3": proj})
        code0, result0, _e0 = self.draft_capability("proj-race3", id="race3-cap", name="race3.py")
        self.assertEqual(code0, 0, result0)
        candidate_id = result0["candidate_id"]

        real_load_catalog = pc.load_catalog
        slow = threading.local()

        def patched(path: Path):
            result = real_load_catalog(path)
            if getattr(slow, "on", False):
                time.sleep(0.5)
            return result

        pc.load_catalog = patched
        self.addCleanup(setattr, pc, "load_catalog", real_load_catalog)

        outcomes: dict[str, tuple[int, dict, str]] = {}

        def approve_worker() -> None:
            slow.on = True
            outcomes["approve"] = run_cli_json(
                [
                    "approve",
                    "--candidate-id",
                    candidate_id,
                    "--catalog",
                    str(self.catalog_path),
                    "--approved-by",
                    "t",
                    "--rationale",
                    "race",
                ]
            )

        def withdraw_worker() -> None:
            slow.on = False
            outcomes["withdraw"] = run_cli_json(
                ["withdraw", "--candidate-id", candidate_id, "--decided-by", "t", "--rationale", "concurrent withdraw attempt"]
            )

        t_approve = threading.Thread(target=approve_worker)
        t_withdraw = threading.Thread(target=withdraw_worker)
        t_approve.start()
        time.sleep(0.15)
        t_withdraw.start()
        t_approve.join(timeout=5)
        t_withdraw.join(timeout=5)

        code_approve, res_approve, _ = outcomes["approve"]
        code_withdraw, res_withdraw, _ = outcomes["withdraw"]
        self.assertEqual(code_approve, 0, res_approve)
        self.assertIn(code_withdraw, (1, 4), res_withdraw)
        self.assertIn(res_withdraw.get("reason"), ("lock_held", "candidate_not_pending"), res_withdraw)

        ledger_lines = (self.promotion_root / pc.LEDGER_NAME).read_text(encoding="utf-8").strip().splitlines()
        actions_for_candidate = [
            entry["action"] for entry in (json.loads(line) for line in ledger_lines) if entry.get("candidate_id") == candidate_id
        ]
        self.assertNotIn("withdraw", actions_for_candidate, actions_for_candidate)
        self.assertIn("approve", actions_for_candidate, actions_for_candidate)

    def test_stale_lock_grants_exactly_one_concurrent_caller(self) -> None:
        # Verification, not a fix: an independent review flagged a possible
        # "two callers can both succeed against a stale lock" race. Real
        # concurrent attempts (both here via threads, and separately via
        # real independent OS processes outside this suite, 40/40 trials)
        # never produced more than one simultaneous acquirer -- os.open's
        # O_CREAT|O_EXCL is atomic at the OS level, and acquire_lock()'s
        # retry loop only permits ONE stale-unlink-and-retry per call. This
        # test pins that property down so a future change to acquire_lock()
        # that broke it would be caught here.
        base_dir = self.tmp / "lock-race-dir"
        base_dir.mkdir()
        lock_path = base_dir / pc.LOCK_NAME
        lock_path.write_text('{"pid": 999999, "started_at": "stale"}', encoding="utf-8")
        stale_time = time.time() - (pc.LOCK_STALE_SECONDS + 1)
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
                pc.acquire_lock(base_dir)
                outcome = "acquired"
            except pc.PromoteFatal as exc:
                outcome = f"failed:{exc.reason}"
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        acquired_count = sum(1 for r in results if r == "acquired")
        self.assertEqual(acquired_count, 1, results)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "permission bits meaningless as root / non-posix")
    def test_readonly_base_dir_raises_named_lock_uncreatable_not_generic_error(self) -> None:
        # Mirrors build_mention_evidence.py's acquire_lock() OSError branch:
        # a read-only base_dir must surface as a NAMED PromoteFatal reason,
        # not propagate as an untyped OSError reported as unexpected_error.
        base_dir = self.tmp / "lock-readonly-dir"
        base_dir.mkdir()
        os.chmod(str(base_dir), 0o500)
        try:
            with self.assertRaises(pc.PromoteFatal) as ctx:
                pc.acquire_lock(base_dir)
            self.assertEqual(ctx.exception.reason, "lock_uncreatable")
        finally:
            os.chmod(str(base_dir), 0o700)


# ---------------------------------------------------------------------------
# Isolation tests -- the critical safety-boundary proof for this delivery
# ---------------------------------------------------------------------------


class IsolationTests(PromoteCapabilityTestCase):
    def test_draft_never_touches_target_project_files_even_when_readonly(self) -> None:
        proj = self.make_project("proj-a")
        _chmod_tree(proj, 0o500)
        self.addCleanup(_chmod_tree, proj, 0o700)
        self.write_catalog({"proj-a": proj})

        before_reusable = (proj / "wiki" / "reusable-capabilities.json").stat().st_mtime_ns
        code, result, _err = self.draft_capability("proj-a")
        self.assertEqual(code, 0, result)
        after_reusable = (proj / "wiki" / "reusable-capabilities.json").stat().st_mtime_ns
        self.assertEqual(before_reusable, after_reusable, "draft must never touch the target project's own files")

    def test_approve_readonly_project_fails_cleanly_no_traceback_no_partial_write(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code0, result0, _e0 = self.draft_capability("proj-a")
        self.assertEqual(code0, 0)
        candidate_id = result0["candidate_id"]

        before = (proj / "wiki" / "reusable-capabilities.json").read_bytes()
        _chmod_tree(proj / "wiki", 0o500)
        self.addCleanup(_chmod_tree, proj, 0o700)

        code, result, err = run_cli_json(
            ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
        )
        # Must fail cleanly with the named permission reason (never a raw
        # PermissionError traceback, never a silent partial write).
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "target_write_permission_denied")
        self.assertNotIn("Traceback", err)

        _chmod_tree(proj / "wiki", 0o700)
        after = (proj / "wiki" / "reusable-capabilities.json").read_bytes()
        self.assertEqual(before, after, "a failed write must never leave a partial change")

    def test_approve_readonly_wiki_dir_gets_named_permission_error(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code0, result0, _e0 = self.draft_capability("proj-a")
        candidate_id = result0["candidate_id"]

        os.chmod(str(proj / "wiki"), 0o500)
        self.addCleanup(os.chmod, str(proj / "wiki"), 0o700)
        try:
            code, result, _err = run_cli_json(
                ["approve", "--candidate-id", candidate_id, "--catalog", str(self.catalog_path), "--approved-by", "t", "--rationale", "r"]
            )
        finally:
            os.chmod(str(proj / "wiki"), 0o700)
        self.assertEqual(code, 4, result)
        self.assertEqual(result["reason"], "target_write_permission_denied")

    def test_reject_withdraw_never_touch_any_project_file(self) -> None:
        proj = self.make_project("proj-a")
        _chmod_tree(proj, 0o500)
        self.addCleanup(_chmod_tree, proj, 0o700)
        self.write_catalog({"proj-a": proj})
        # draft must work read-only; candidate lives entirely in
        # PROMOTION_ROOT, never inside `proj`.
        code0, result0, _e0 = self.draft_capability("proj-a")
        self.assertEqual(code0, 0)
        code, result, _err = run_cli_json(["reject", "--candidate-id", result0["candidate_id"], "--decided-by", "t", "--rationale", "r"])
        self.assertEqual(code, 0, result)


class ExitCodeCoverageTests(PromoteCapabilityTestCase):
    """A focused sweep asserting every documented exit code (0/1/2/4) is
    reachable for the decision-class contract this tool implements."""

    def test_exit_0_success(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code, _r, _e = self.draft_capability("proj-a")
        self.assertEqual(code, 0)

    def test_exit_1_validation_failed(self) -> None:
        self.write_catalog({})
        code, result, _e = run_cli_json(["reject", "--candidate-id", "cand-nope", "--decided-by", "t", "--rationale", "r"])
        self.assertEqual(code, 1)

    def test_exit_2_usage_error(self) -> None:
        proj = self.make_project("proj-a")
        self.write_catalog({"proj-a": proj})
        code0, result0, _e0 = self.draft_capability("proj-a")
        code, _r, _e = run_cli_json(
            ["approve", "--candidate-id", result0["candidate_id"], "--catalog", "  ", "--approved-by", "t", "--rationale", "r"]
        )
        self.assertEqual(code, 2)

    def test_exit_4_environment_broken(self) -> None:
        code, result, _e = run_cli_json(["draft", "--from-json", str(self.tmp / "x.json"), "--catalog", str(self.tmp / "missing-catalog.json")])
        # from-json missing is checked first (usage, exit 2); use a real
        # input file but a missing catalog to reach the catalog-fatal path.
        input_path = self.tmp / "input.json"
        input_path.write_text(
            json.dumps(
                {
                    "target_project": "proj-a",
                    "proposed_target": "reusable-capabilities.json",
                    "id": "x",
                    "kind": "script",
                    "name": "n.py",
                    "path": "p.py",
                    "summary": "s",
                    "source": {"mechanism": "human"},
                }
            ),
            encoding="utf-8",
        )
        code2, result2, _e2 = run_cli_json(["draft", "--from-json", str(input_path), "--catalog", str(self.tmp / "missing-catalog.json")])
        self.assertEqual(code2, 4)
        self.assertEqual(result2["reason"], "catalog_missing")


class InvokeWikiEditGuardStaleGuardReasonTests(unittest.TestCase):
    """round-6-fix P2-1 (dedicated review): a stale, co-deployed guard that
    doesn't understand --wiki-dir-fd/--wiki-name argparse-errors on exit 2
    with an empty stdout and the real explanation on stderr. body.get is
    still a valid call on the resulting {} and returns None, which is NOT
    the same thing as "no reason to report" -- proc.stderr must be
    consulted whenever the guard's own JSON body didn't carry one, or the
    operator sees a bare "wiki_edit_guard_refused" with no message at all,
    misread as their own write being rejected rather than a stale binary."""

    def test_empty_stdout_on_exit_2_falls_back_to_stderr(self) -> None:
        class FakeCompletedProcess:
            returncode = 2
            stdout = ""
            stderr = "wiki_edit_guard.py: error: unrecognized arguments: --wiki-dir-fd 3 --wiki-name w.json\n"

        with mock.patch.object(pc.subprocess, "run", return_value=FakeCompletedProcess()):
            with self.assertRaises(pc.PromoteValidationError) as ctx:
                pc.invoke_wiki_edit_guard(
                    Path("/nonexistent/wiki_edit_guard.py"),
                    Path("/nonexistent/wiki/orca-context-wiki.json"),
                    {"meta": {"content_version": 1, "updated_at": "2026-01-01T00:00:00Z"}},
                )
        self.assertEqual(ctx.exception.reason, "wiki_edit_guard_refused")
        self.assertIsNotNone(ctx.exception.details)
        self.assertIn("unrecognized arguments", ctx.exception.details)

    def test_non_json_stdout_on_exit_2_falls_back_to_stderr(self) -> None:
        class FakeCompletedProcess:
            returncode = 2
            stdout = "not json at all"
            stderr = "some other real refusal reason\n"

        with mock.patch.object(pc.subprocess, "run", return_value=FakeCompletedProcess()):
            with self.assertRaises(pc.PromoteValidationError) as ctx:
                pc.invoke_wiki_edit_guard(
                    Path("/nonexistent/wiki_edit_guard.py"),
                    Path("/nonexistent/wiki/orca-context-wiki.json"),
                    {"meta": {"content_version": 1, "updated_at": "2026-01-01T00:00:00Z"}},
                )
        self.assertIn("some other real refusal reason", ctx.exception.details)

    def test_real_reason_in_json_body_still_wins_over_stderr(self) -> None:
        class FakeCompletedProcess:
            returncode = 2
            stdout = json.dumps({"ok": False, "reason": "the real guard refusal reason"})
            stderr = "should not be surfaced when body already has a reason\n"

        with mock.patch.object(pc.subprocess, "run", return_value=FakeCompletedProcess()):
            with self.assertRaises(pc.PromoteValidationError) as ctx:
                pc.invoke_wiki_edit_guard(
                    Path("/nonexistent/wiki_edit_guard.py"),
                    Path("/nonexistent/wiki/orca-context-wiki.json"),
                    {"meta": {"content_version": 1, "updated_at": "2026-01-01T00:00:00Z"}},
                )
        self.assertEqual(ctx.exception.details, "the real guard refusal reason")


class AtomicWriteInDirShortWriteRegressionTests(unittest.TestCase):
    """round-6-fix P1 (final-gate dedicated review, Grok, 2026-08-25): the
    sibling short-write bug found in wiki_edit_guard.py's
    _atomic_write_via_dir_fd() -- an unchecked os.write(fd, payload) that
    silently accepts a short write and still renames the truncated tmp
    file onto the live target -- was copied into THIS file's own
    atomic_write_in_dir() when wiki_edit_guard.py's dir-fd technique was
    written by mirroring this exact function. Fixed identically via
    _write_all_bytes()."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="promote-cap-shortwrite-"))
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
                return real_write(fd_, data[:40])
            return 0

        with mock.patch.object(pc.os, "write", side_effect=short_write):
            with self.assertRaises(OSError):
                pc._write_all_bytes(fd, payload)
        os.close(fd)

    def test_atomic_write_in_dir_refuses_rather_than_truncates_on_short_write(self) -> None:
        target_dir = self.tmp / "wiki"
        target_dir.mkdir()
        filename = "reusable-capabilities.json"
        final_path = target_dir / filename
        original_bytes = b'{"schema_version": 1, "capabilities": []}\n'
        final_path.write_bytes(original_bytes)

        new_payload = b'{"schema_version": 1, "capabilities": [{"id": "x"}]}\n'
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[: max(1, len(data) - 20)])
            return 0

        dir_fd = os.open(str(target_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with mock.patch.object(pc.os, "write", side_effect=short_write):
                with self.assertRaises(OSError):
                    pc.atomic_write_in_dir(final_path, new_payload, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)

        # The live target file must be COMPLETELY untouched, and no
        # leftover tmp file next to it.
        self.assertEqual(final_path.read_bytes(), original_bytes)
        leftovers = [p for p in target_dir.iterdir() if p.name != filename]
        self.assertEqual(leftovers, [], f"leftover tmp files: {leftovers}")

    def test_atomic_write_in_dir_path_mode_also_refuses_on_short_write(self) -> None:
        # Same regression, non-dir_fd (plain path) branch of the function.
        target_dir = self.tmp / "knowledge"
        target_dir.mkdir()
        final_path = target_dir / "notes.md"
        original_bytes = b"# original\n"
        final_path.write_bytes(original_bytes)

        new_payload = b"# a brand new, longer replacement body\n"
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[: max(1, len(data) - 10)])
            return 0

        with mock.patch.object(pc.os, "write", side_effect=short_write):
            with self.assertRaises(OSError):
                pc.atomic_write_in_dir(final_path, new_payload)

        self.assertEqual(final_path.read_bytes(), original_bytes)
        leftovers = [p for p in target_dir.iterdir() if p.name != "notes.md"]
        self.assertEqual(leftovers, [], f"leftover tmp files: {leftovers}")


class RemainingShortWriteSitesRegressionTests(unittest.TestCase):
    """2026-08-25, found by Grok's re-gate of the atomic_write_in_dir/
    _atomic_write_via_dir_fd fix above: this module has THREE more
    call sites sharing the identical unchecked-os.write() shape --
    atomic_write_within() (candidate record JSON under PROMOTION_ROOT),
    acquire_lock() (the lock-file bookkeeping stamp), and append_ledger()
    (the audit-trail JSONL). None of these were the originally-reported P1
    (that was specifically the wiki file, via atomic_write_in_dir), but
    they are the same bug class in the same file and get the same fix."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="promote-cap-remaining-shortwrite-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def test_atomic_write_within_refuses_rather_than_truncates(self) -> None:
        base_dir = self.tmp / "records"
        base_dir.mkdir()
        final_path = base_dir / "cand-x.json"
        original_bytes = b'{"status": "pending_approval"}\n'
        final_path.write_bytes(original_bytes)

        new_payload = b'{"status": "approved", "approved_by": "someone"}\n'
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[: max(1, len(data) - 15)])
            return 0

        with mock.patch.object(pc.os, "write", side_effect=short_write):
            with self.assertRaises(OSError):
                pc.atomic_write_within(base_dir, final_path, new_payload)

        self.assertEqual(final_path.read_bytes(), original_bytes)
        leftovers = [p for p in base_dir.iterdir() if p.name != "cand-x.json"]
        self.assertEqual(leftovers, [], f"leftover tmp files: {leftovers}")

    def test_acquire_lock_refuses_rather_than_leaves_truncated_stamp(self) -> None:
        base_dir = self.tmp / "lockdir"
        base_dir.mkdir()
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[:5])
            return 0

        with mock.patch.object(pc.os, "write", side_effect=short_write):
            with self.assertRaises(pc.PromoteFatal):
                pc.acquire_lock(base_dir)

        # No lock file left behind in a half-written state -- os.open()
        # already created it, but acquire_lock's own OSError branch here
        # does not attempt cleanup of the lock file itself (unlike the
        # tmp+rename writers, there is no separate tmp name); assert only
        # what the fix actually changes: it must not silently report
        # success with a truncated stamp, and a second real attempt must
        # not be blocked forever by a corrupt-but-present lock file older
        # than LOCK_STALE_SECONDS having been created. Here we only assert
        # the immediate failure is clean and named.
        lock_path = base_dir / pc.LOCK_NAME
        if lock_path.exists():
            content = lock_path.read_bytes()
            # Whatever partial bytes exist (if any survived the mocked
            # short write), they must not be reported as a successful lock
            # acquisition by the caller -- already covered by assertRaises
            # above; this is just documenting the on-disk state honestly.
            self.assertLessEqual(len(content), 5)

    def test_append_ledger_short_write_raises_instead_of_silent_truncation(self) -> None:
        root = self.tmp / "promo-root"
        root.mkdir()
        entry = {"event": "approved", "candidate_id": "cand-y", "detail": "x" * 40}
        real_write = os.write
        calls = {"n": 0}

        def short_write(fd_, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd_, data[: max(1, len(data) - 10)])
            return 0

        with mock.patch.object(pc.os, "write", side_effect=short_write):
            with self.assertRaises(OSError):
                pc.append_ledger(root, entry)

    def test_write_all_bytes_loops_across_all_three_call_sites_normal_case(self) -> None:
        # Sanity: the normal (non-adversarial) path for all three functions
        # still works byte-for-byte after routing through _write_all_bytes.
        base_dir = self.tmp / "normal"
        base_dir.mkdir()
        final_path = base_dir / "cand-z.json"
        payload = b'{"status": "approved"}\n'
        pc.atomic_write_within(base_dir, final_path, payload)
        self.assertEqual(final_path.read_bytes(), payload)

        lock_dir = self.tmp / "normal-lock"
        lock_dir.mkdir()
        lock_path = pc.acquire_lock(lock_dir)
        self.assertTrue(lock_path.is_file())
        pc.release_lock(lock_path)

        ledger_root = self.tmp / "normal-ledger"
        ledger_root.mkdir()
        pc.append_ledger(ledger_root, {"event": "test"})
        ledger_path = ledger_root / pc.LEDGER_NAME
        self.assertTrue(ledger_path.is_file())
        self.assertIn("test", ledger_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
