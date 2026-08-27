#!/usr/bin/env python3
"""Tests for install_shared.py's --update deletion-guard behavior.

Background (2026-08-26 incident): --update replaces the deployed
destination directory with a fresh copytree of this repo's source tree.
Because that replace previously had no merge/allowlist logic, three live
files that existed only in the deployed directory (not in this repo's
tracked scripts/) were silently deleted. This suite exercises the guard
added to prevent a repeat: --update must refuse and list orphaned files
unless --allow-delete is explicitly passed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent / "install_shared.py"

spec = importlib.util.spec_from_file_location("install_shared", SCRIPT_PATH)
install_shared = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(install_shared)


def _make_source_tree(root: Path) -> Path:
    source = root / "repo-source"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: orca-context-bridge\n---\nbody\n", encoding="utf-8"
    )
    (source / "scripts" / "kept.py").write_text("print('kept')\n", encoding="utf-8")
    return source


class TestFilesOnlyInDestination:
    def test_no_destination_returns_empty(self, tmp_path: Path) -> None:
        destination = tmp_path / "missing"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        assert install_shared.files_only_in_destination(destination, incoming) == []

    def test_reports_files_missing_from_incoming(self, tmp_path: Path) -> None:
        destination = tmp_path / "dest"
        incoming = tmp_path / "incoming"
        (destination / "scripts").mkdir(parents=True)
        (incoming / "scripts").mkdir(parents=True)
        (destination / "scripts" / "kept.py").write_text("x", encoding="utf-8")
        (destination / "scripts" / "orphan_one.py").write_text("x", encoding="utf-8")
        (destination / "orphan_two.md").write_text("x", encoding="utf-8")
        (incoming / "scripts" / "kept.py").write_text("x", encoding="utf-8")

        result = install_shared.files_only_in_destination(destination, incoming)

        assert result == ["orphan_two.md", "scripts/orphan_one.py"]

    def test_no_orphans_when_everything_present(self, tmp_path: Path) -> None:
        destination = tmp_path / "dest"
        incoming = tmp_path / "incoming"
        (destination / "scripts").mkdir(parents=True)
        (incoming / "scripts").mkdir(parents=True)
        (destination / "scripts" / "kept.py").write_text("x", encoding="utf-8")
        (incoming / "scripts" / "kept.py").write_text("x", encoding="utf-8")

        assert install_shared.files_only_in_destination(destination, incoming) == []


class TestUpdateCli:
    """End-to-end CLI runs against a fake HOME so the real ~/.agents etc are untouched."""

    def _setup(self, tmp_path: Path):
        source = _make_source_tree(tmp_path)
        shared_root = tmp_path / "home" / ".agents" / "skills"
        claude_root = tmp_path / "home" / ".claude" / "skills"
        codex_root = tmp_path / "home" / ".codex" / "skills"
        shared_root.mkdir(parents=True)
        return source, shared_root, claude_root, codex_root

    def test_update_refuses_and_lists_orphans_without_allow_delete(self, tmp_path):
        source, shared_root, claude_root, codex_root = self._setup(tmp_path)
        # Patch the script's own repo-root detection: install_shared.py derives
        # `source` from its own file location (parent.parent), so run it via a
        # copy placed inside the fake source tree.
        script_copy = source / "scripts" / "install_shared.py"
        script_copy.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")

        def run(args):
            return subprocess.run(
                [sys.executable, str(script_copy), *args,
                 "--shared-root", str(shared_root),
                 "--claude-root", str(claude_root),
                 "--codex-root", str(codex_root)],
                capture_output=True,
                text=True,
                check=False,
            )

        # Initial install.
        first = run([])
        assert first.returncode == 0, first.stdout + first.stderr

        deployed = shared_root / "orca-context-bridge"
        assert deployed.exists()

        # Simulate a live-only file that never made it into this repo's source
        # tree (the exact shape of the 2026-08-26 incident).
        live_only = deployed / "scripts" / "build_startup_bundle.py"
        live_only.write_text("print('live only, not in source')\n", encoding="utf-8")

        second = run(["--update"])
        assert second.returncode == 3, second.stdout + second.stderr
        payload = json.loads(second.stdout)
        assert payload["ok"] is False
        assert payload["error"] == "update_would_delete_files"
        assert "scripts/build_startup_bundle.py" in payload["files"]

        # Refusal must not have touched the deployed file.
        assert live_only.exists()
        assert live_only.read_text(encoding="utf-8") == "print('live only, not in source')\n"

    def test_update_with_allow_delete_proceeds_and_deletes(self, tmp_path):
        source, shared_root, claude_root, codex_root = self._setup(tmp_path)
        script_copy = source / "scripts" / "install_shared.py"
        script_copy.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")

        def run(args):
            return subprocess.run(
                [sys.executable, str(script_copy), *args,
                 "--shared-root", str(shared_root),
                 "--claude-root", str(claude_root),
                 "--codex-root", str(codex_root)],
                capture_output=True,
                text=True,
                check=False,
            )

        first = run([])
        assert first.returncode == 0, first.stdout + first.stderr

        deployed = shared_root / "orca-context-bridge"
        live_only = deployed / "scripts" / "build_startup_bundle.py"
        live_only.write_text("print('live only, not in source')\n", encoding="utf-8")

        second = run(["--update", "--allow-delete"])
        assert second.returncode == 0, second.stdout + second.stderr
        # Two JSON objects are printed on the allow-delete path: the
        # deletion-warning record, then the final result record.
        decoder = json.JSONDecoder()
        text = second.stdout
        objects = []
        idx = 0
        while idx < len(text):
            text_from = text[idx:].lstrip()
            if not text_from:
                break
            idx += len(text[idx:]) - len(text_from)
            obj, end = decoder.raw_decode(text, idx)
            objects.append(obj)
            idx = end
        assert len(objects) == 2, objects
        warning, result = objects
        assert warning["warning"] == "deleting_files_not_in_source"
        assert "scripts/build_startup_bundle.py" in warning["files"]
        assert result["ok"] is True

        assert not live_only.exists()

    def test_update_normal_path_unchanged_when_no_orphans(self, tmp_path):
        source, shared_root, claude_root, codex_root = self._setup(tmp_path)
        script_copy = source / "scripts" / "install_shared.py"
        script_copy.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")

        def run(args):
            return subprocess.run(
                [sys.executable, str(script_copy), *args,
                 "--shared-root", str(shared_root),
                 "--claude-root", str(claude_root),
                 "--codex-root", str(codex_root)],
                capture_output=True,
                text=True,
                check=False,
            )

        first = run([])
        assert first.returncode == 0, first.stdout + first.stderr

        # Modify source and update without introducing any deployed-only files.
        (source / "scripts" / "kept.py").write_text("print('kept v2')\n", encoding="utf-8")

        second = run(["--update"])
        assert second.returncode == 0, second.stdout + second.stderr
        payload = json.loads(second.stdout)
        assert payload["ok"] is True
        assert payload["action"] == "update"

        deployed_file = shared_root / "orca-context-bridge" / "scripts" / "kept.py"
        assert deployed_file.read_text(encoding="utf-8") == "print('kept v2')\n"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
