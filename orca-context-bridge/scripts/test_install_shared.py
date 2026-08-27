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


class TestNodeKind:
    """``node_kind`` must report the node itself, never what a symlink points at."""

    def test_plain_file_and_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a_file").write_text("x", encoding="utf-8")
        (tmp_path / "a_dir").mkdir()
        assert install_shared.node_kind(tmp_path / "a_file") == "file"
        assert install_shared.node_kind(tmp_path / "a_dir") == "directory"

    def test_absent_path(self, tmp_path: Path) -> None:
        assert install_shared.node_kind(tmp_path / "nope") == "absent"

    def test_symlinks_are_not_reported_as_their_targets(self, tmp_path: Path) -> None:
        (tmp_path / "target_file").write_text("x", encoding="utf-8")
        (tmp_path / "target_dir").mkdir()
        (tmp_path / "link_to_file").symlink_to(tmp_path / "target_file")
        (tmp_path / "link_to_dir").symlink_to(tmp_path / "target_dir", target_is_directory=True)
        (tmp_path / "dangling").symlink_to(tmp_path / "gone")

        assert install_shared.node_kind(tmp_path / "link_to_file") == "symlink"
        assert install_shared.node_kind(tmp_path / "link_to_dir") == "symlink"
        assert install_shared.node_kind(tmp_path / "dangling") == "symlink"


class TestNodeTypeChanges:
    """A path present on both sides is still destroyed when its node type changes.

    ``exists()`` answers "present" for a file and a directory alike, so the
    original guard let every one of these through with an empty deletion list.
    """

    def test_deployed_file_becomes_source_directory(self, tmp_path: Path) -> None:
        destination = tmp_path / "dest"
        incoming = tmp_path / "incoming"
        destination.mkdir()
        incoming.mkdir()
        (destination / "collide").write_text("live content\n", encoding="utf-8")
        (incoming / "collide").mkdir()
        (incoming / "collide" / "inner.txt").write_text("x", encoding="utf-8")

        assert install_shared.destination_changes(destination, incoming) == [
            {
                "path": "collide",
                "reason": "type_change",
                "deployed": "file",
                "source": "directory",
            }
        ]

    def test_deployed_directory_becomes_source_file(self, tmp_path: Path) -> None:
        destination = tmp_path / "dest"
        incoming = tmp_path / "incoming"
        (destination / "collide").mkdir(parents=True)
        incoming.mkdir()
        (destination / "collide" / "live.txt").write_text("live\n", encoding="utf-8")
        (incoming / "collide").write_text("now a file\n", encoding="utf-8")

        # The directory itself is reported once; its contents are not
        # re-listed, because the whole subtree goes with it.
        assert install_shared.destination_changes(destination, incoming) == [
            {
                "path": "collide",
                "reason": "type_change",
                "deployed": "directory",
                "source": "file",
            }
        ]

    def test_deployed_empty_directory_becomes_source_file(self, tmp_path: Path) -> None:
        destination = tmp_path / "dest"
        incoming = tmp_path / "incoming"
        (destination / "collide").mkdir(parents=True)
        incoming.mkdir()
        (incoming / "collide").write_text("now a file\n", encoding="utf-8")

        assert install_shared.files_only_in_destination(destination, incoming) == ["collide"]

    def test_deployed_symlink_to_directory_absent_from_source(self, tmp_path: Path) -> None:
        destination = tmp_path / "dest"
        incoming = tmp_path / "incoming"
        outside = tmp_path / "outside"
        destination.mkdir()
        incoming.mkdir()
        outside.mkdir()
        (outside / "payload.txt").write_text("payload\n", encoding="utf-8")
        (destination / "linkdir").symlink_to(outside, target_is_directory=True)

        # is_dir() follows symlinks, so the original guard skipped this
        # entirely and reported nothing at all.
        assert install_shared.destination_changes(destination, incoming) == [
            {
                "path": "linkdir",
                "reason": "missing_from_source",
                "deployed": "symlink",
                "source": "absent",
            }
        ]

    def test_deployed_symlink_replaced_by_plain_source_file(self, tmp_path: Path) -> None:
        destination = tmp_path / "dest"
        incoming = tmp_path / "incoming"
        destination.mkdir()
        incoming.mkdir()
        (tmp_path / "target.txt").write_text("target\n", encoding="utf-8")
        (destination / "thing").symlink_to(tmp_path / "target.txt")
        (incoming / "thing").write_text("plain\n", encoding="utf-8")

        assert install_shared.destination_changes(destination, incoming) == [
            {
                "path": "thing",
                "reason": "type_change",
                "deployed": "symlink",
                "source": "file",
            }
        ]

    def test_symlink_on_both_sides_is_an_ordinary_overwrite(self, tmp_path: Path) -> None:
        destination = tmp_path / "dest"
        incoming = tmp_path / "incoming"
        destination.mkdir()
        incoming.mkdir()
        (destination / "thing").symlink_to(tmp_path / "one")
        (incoming / "thing").symlink_to(tmp_path / "two")

        assert install_shared.destination_changes(destination, incoming) == []

    def test_symlinked_destination_root_is_reported(self, tmp_path: Path) -> None:
        real = tmp_path / "real-install"
        real.mkdir()
        (real / "SKILL.md").write_text("x", encoding="utf-8")
        destination = tmp_path / "dest"
        destination.symlink_to(real, target_is_directory=True)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        (incoming / "SKILL.md").write_text("x", encoding="utf-8")

        assert install_shared.destination_changes(destination, incoming) == [
            {
                "path": ".",
                "reason": "type_change",
                "deployed": "symlink",
                "source": "directory",
            }
        ]

    def test_describe_changes_says_what_kind_of_change_it_is(self, tmp_path: Path) -> None:
        described = install_shared.describe_changes([
            {
                "path": "scripts/live.py",
                "reason": "type_change",
                "deployed": "file",
                "source": "directory",
            },
            {
                "path": "scripts/gone.py",
                "reason": "missing_from_source",
                "deployed": "file",
                "source": "absent",
            },
        ])

        assert described == [
            "scripts/live.py: deployed file becomes directory in source",
            "scripts/gone.py: deployed file is absent from source",
        ]


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

    def test_update_refuses_when_deployed_file_becomes_source_directory(self, tmp_path):
        """The reviewed P1: exists() sees the path on both sides, so the guard
        stayed silent while the replace destroyed the live file's content."""
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

        assert run([]).returncode == 0

        deployed = shared_root / "orca-context-bridge"
        live = deployed / "scripts" / "live_config.py"
        live.write_text("SECRET_LIVE_VALUE = 1\n", encoding="utf-8")
        # The same path is a directory in the incoming source tree.
        (source / "scripts" / "live_config.py").mkdir()
        (source / "scripts" / "live_config.py" / "__init__.py").write_text(
            "# now a package\n", encoding="utf-8"
        )

        second = run(["--update"])
        assert second.returncode == 3, second.stdout + second.stderr
        payload = json.loads(second.stdout)
        assert payload["error"] == "update_would_delete_files"
        assert "scripts/live_config.py" in payload["files"]
        assert {
            "path": "scripts/live_config.py",
            "reason": "type_change",
            "deployed": "file",
            "source": "directory",
        } in payload["changes"]
        assert (
            "scripts/live_config.py: deployed file becomes directory in source"
            in payload["details"]
        )

        # The live file must be untouched by the refusal.
        assert live.is_file()
        assert live.read_text(encoding="utf-8") == "SECRET_LIVE_VALUE = 1\n"

    def test_update_refuses_when_deployed_directory_becomes_source_file(self, tmp_path):
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

        assert run([]).returncode == 0

        deployed = shared_root / "orca-context-bridge"
        live_dir = deployed / "scripts" / "live_pkg"
        live_dir.mkdir()
        (live_dir / "data.json").write_text('{"live": true}\n', encoding="utf-8")
        # The same path is a plain file in the incoming source tree.
        (source / "scripts" / "live_pkg").write_text("# now a module\n", encoding="utf-8")

        second = run(["--update"])
        assert second.returncode == 3, second.stdout + second.stderr
        payload = json.loads(second.stdout)
        assert payload["error"] == "update_would_delete_files"
        assert {
            "path": "scripts/live_pkg",
            "reason": "type_change",
            "deployed": "directory",
            "source": "file",
        } in payload["changes"]

        assert (live_dir / "data.json").read_text(encoding="utf-8") == '{"live": true}\n'

    def test_update_refuses_deployed_symlink_missing_from_source(self, tmp_path):
        """is_dir() follows symlinks, so a deployed symlink-to-directory used to
        be skipped outright and deleted with no warning at all."""
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

        assert run([]).returncode == 0

        deployed = shared_root / "orca-context-bridge"
        outside = tmp_path / "outside-payload"
        outside.mkdir()
        (outside / "payload.txt").write_text("payload\n", encoding="utf-8")
        link = deployed / "scripts" / "linked_data"
        link.symlink_to(outside, target_is_directory=True)

        second = run(["--update"])
        assert second.returncode == 3, second.stdout + second.stderr
        payload = json.loads(second.stdout)
        assert {
            "path": "scripts/linked_data",
            "reason": "missing_from_source",
            "deployed": "symlink",
            "source": "absent",
        } in payload["changes"]

        assert link.is_symlink()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
