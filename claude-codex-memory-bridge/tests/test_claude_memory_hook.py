from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

import claude_memory_hook as hook


TEST_UUID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"

# The workspace the fixture pretends every Codex session runs from by default.
# Deliberately contains a space and a '/' so tests exercise the real
# character-replacement transform, not just a already-alphanumeric path.
DEFAULT_CWD = "/Users/tester/Orca Workspace/project-a"


class HookFixture:
    def __init__(self, cwd: str = DEFAULT_CWD) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "claude-projects"
        self.runtime = self.root / "runtime"
        self.source.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        source_script = Path(hook.__file__).read_bytes()
        self.script = self.runtime / "claude_memory_hook.py"
        self.script.write_bytes(source_script)
        self.script.chmod(0o600)
        self.script_sha = hashlib.sha256(source_script).hexdigest()
        self.cwd = cwd
        self.project_dirname = hook.claude_project_dirname(cwd)

    def add_memory(self, text: str, *, cwd: str | None = None, mode: int = 0o600) -> Path:
        dirname = hook.claude_project_dirname(cwd) if cwd is not None else self.project_dirname
        directory = self.source / dirname / "memory"
        directory.mkdir(mode=0o700, parents=True)
        path = directory / "MEMORY.md"
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def policy(self, **limit_overrides: int) -> tuple[Path, str]:
        limits = {
            "max_files": 8,
            "max_file_bytes": 32_768,
            "max_total_bytes": 65_536,
            "max_blocks": 4,
            "max_output_bytes": 7_000,
        }
        limits.update(limit_overrides)
        payload = {
            "schema": hook.POLICY_SCHEMA,
            "bridge_id": hook.BRIDGE_ID,
            "enabled": True,
            "consumer": "codex",
            "ssd_root": os.fspath(self.root),
            "volume_uuid": TEST_UUID,
            "source_root": os.fspath(self.source),
            "runtime_root": os.fspath(self.runtime),
            "limits": limits,
        }
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        path = self.runtime / "policy.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        return path, hashlib.sha256(raw).hexdigest()

    def run(self, prompt: str, *, cwd: str | None = None, **limit_overrides: int) -> str:
        policy, policy_sha = self.policy(**limit_overrides)
        stdin = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": prompt,
                "cwd": cwd if cwd is not None else self.cwd,
            }
        ).encode()
        return hook.run(
            policy_path=policy,
            expected_policy_sha256=policy_sha,
            expected_script_sha256=self.script_sha,
            stdin=stdin,
            script_path=self.script,
            volume_uuid_reader=lambda _root: TEST_UUID,
        )

    def close(self) -> None:
        self.temp.cleanup()


class ClaudeMemoryHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = HookFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_emits_only_query_relevant_untrusted_context(self) -> None:
        self.fixture.add_memory(
            "# SSH route gate\nUse the reviewed SSH alias and verify the static route gate.\n\n"
            "# UI colors\nThe button is violet.\n",
        )
        output = self.fixture.run("请检查 SSH route gate")
        payload = json.loads(output)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ORCA_CLAUDE_NATIVE_MEMORY_CONTEXT_V1", context)
        self.assertIn("untrusted_reference_only", context)
        self.assertIn("reviewed SSH alias", context)
        self.assertNotIn("button is violet", context)
        self.assertNotIn("project-a", context)

    def test_redacts_credentials_ips_and_home_paths(self) -> None:
        self.fixture.add_memory(
            "# deployment token\n"
            "token=ghp_abcdefghijklmnopqrstuvwxyz1234567890 and password=hunter2 "
            "host 203.0.113.7 in /Users/alice/private for alice@example.com.\n",
        )
        output = self.fixture.run("deployment token host password")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("hunter2", context)
        self.assertNotIn("ghp_", context)
        self.assertNotIn("203.0.113.7", context)
        self.assertNotIn("/Users/alice", context)
        self.assertNotIn("alice@example.com", context)
        self.assertIn("[REDACTED]", context)
        self.assertIn("[REDACTED_IP]", context)
        self.assertIn("[REDACTED_EMAIL]", context)

    def test_skips_symlinked_memory(self) -> None:
        symlink_cwd = "/Users/tester/symlink-project"
        external = self.root_external_memory("# forbidden topic\nsecret detail")
        symlink_dir = self.fixture.source / hook.claude_project_dirname(symlink_cwd) / "memory"
        symlink_dir.mkdir(mode=0o700, parents=True)
        (symlink_dir / "MEMORY.md").symlink_to(external)
        self.assertEqual(self.fixture.run("forbidden topic", cwd=symlink_cwd), "")

    def test_skips_group_writable_memory(self) -> None:
        writable_cwd = "/Users/tester/writable-project"
        self.fixture.add_memory(
            "# forbidden topic\nwritable detail", cwd=writable_cwd, mode=0o620
        )
        self.assertEqual(self.fixture.run("forbidden topic", cwd=writable_cwd), "")

    def root_external_memory(self, text: str) -> Path:
        path = self.fixture.root / "external.md"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_rejects_duplicate_hook_input_keys(self) -> None:
        policy, policy_sha = self.fixture.policy()
        raw = b'{"hook_event_name":"UserPromptSubmit","prompt":"one","prompt":"two"}'
        with self.assertRaises(hook.BridgeError):
            hook.run(
                policy_path=policy,
                expected_policy_sha256=policy_sha,
                expected_script_sha256=self.fixture.script_sha,
                stdin=raw,
                script_path=self.fixture.script,
                volume_uuid_reader=lambda _root: TEST_UUID,
            )

    def test_fails_closed_on_volume_uuid_mismatch(self) -> None:
        self.fixture.add_memory("# topic\ndetail")
        policy, policy_sha = self.fixture.policy()
        raw = json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "topic", "cwd": self.fixture.cwd}
        ).encode()
        with self.assertRaises(hook.BridgeError):
            hook.run(
                policy_path=policy,
                expected_policy_sha256=policy_sha,
                expected_script_sha256=self.fixture.script_sha,
                stdin=raw,
                script_path=self.fixture.script,
                volume_uuid_reader=lambda _root: "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
            )

    def test_context_respects_utf8_byte_limit(self) -> None:
        self.fixture.add_memory("# 服务器记忆\n" + "服务器迁移证据。" * 1_000)
        output = self.fixture.run("服务器记忆迁移", max_output_bytes=512)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context.encode("utf-8")), 512)
        records = [line[2:] for line in context.splitlines() if line.startswith("- ")]
        self.assertTrue(records)
        for record in records:
            parsed = json.loads(record)
            self.assertEqual(set(parsed), {"project", "section", "quoted_text"})

    # --- workspace-scoping (cross-project memory leak fix) -----------------

    def test_claude_project_dirname_matches_known_real_mapping(self) -> None:
        # This is Claude Code's actual, empirically-verified project-directory
        # naming transform (confirmed character-for-character against this
        # session's own real ~/.claude/projects/ mapping) -- every character
        # that is not ASCII alphanumeric becomes exactly one '-', including
        # each character of a multi-character CJK run, with no collapsing.
        self.assertEqual(
            hook.claude_project_dirname(
                "/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca"
            ),
            "-Volumes-Extreme-SSD-Orca-workspaces-orca---orca",
        )
        self.assertEqual(
            hook.claude_project_dirname(
                "/Volumes/Extreme SSD/Orca/workspaces/orca/codex-restore-tool"
            ),
            "-Volumes-Extreme-SSD-Orca-workspaces-orca-codex-restore-tool",
        )

    def test_only_returns_memory_for_the_requesting_workspace(self) -> None:
        # The bug this guards against: read_memory_documents() used to scan
        # every Claude project under source_root and return all of them,
        # regardless of which workspace the Codex session calling this hook
        # was actually in -- any project's Codex session could read any other
        # project's Claude memory. Two workspaces exist here; a request from
        # workspace A's cwd must never see workspace B's content or identity.
        other_cwd = "/Users/tester/other-workspace"
        self.fixture.add_memory("# shared topic\nworkspace A detail")
        self.fixture.add_memory("# shared topic\nworkspace B detail", cwd=other_cwd)
        output = self.fixture.run("shared topic")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("workspace A detail", context)
        self.assertNotIn("workspace B detail", context)

    def test_fails_closed_when_cwd_is_missing(self) -> None:
        self.fixture.add_memory("# topic\ndetail")
        policy, policy_sha = self.fixture.policy()
        raw = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "topic"}).encode()
        with self.assertRaises(hook.BridgeError):
            hook.run(
                policy_path=policy,
                expected_policy_sha256=policy_sha,
                expected_script_sha256=self.fixture.script_sha,
                stdin=raw,
                script_path=self.fixture.script,
                volume_uuid_reader=lambda _root: TEST_UUID,
            )

    def test_fails_closed_when_cwd_is_not_an_absolute_path(self) -> None:
        self.fixture.add_memory("# topic\ndetail")
        policy, policy_sha = self.fixture.policy()
        raw = json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "topic", "cwd": "relative/path"}
        ).encode()
        with self.assertRaises(hook.BridgeError):
            hook.run(
                policy_path=policy,
                expected_policy_sha256=policy_sha,
                expected_script_sha256=self.fixture.script_sha,
                stdin=raw,
                script_path=self.fixture.script,
                volume_uuid_reader=lambda _root: TEST_UUID,
            )

    def test_unknown_workspace_yields_no_context_without_error(self) -> None:
        # A cwd with no matching Claude project directory at all (e.g. a
        # brand-new workspace with no Claude history yet) is an expected,
        # benign steady state, not a bridge failure -- it must not raise.
        self.fixture.add_memory("# topic\ndetail")
        output = self.fixture.run("topic", cwd="/Users/tester/never-seen-workspace")
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
