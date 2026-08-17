from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
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

    def add_memory(
        self,
        text: str,
        *,
        cwd: str | None = None,
        mode: int = 0o600,
        with_transcript: bool = True,
    ) -> Path:
        effective_cwd = cwd if cwd is not None else self.cwd
        dirname = hook.claude_project_dirname(effective_cwd)
        directory = self.source / dirname / "memory"
        directory.mkdir(mode=0o700, parents=True)
        path = directory / "MEMORY.md"
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        if with_transcript:
            # Real Claude Code only trusts a project directory for a cwd once
            # a session transcript inside it records that exact cwd (see
            # claude_memory_hook.py's _session_recorded_cwd_matches); the
            # fixture reproduces that by default so tests about memory
            # *content* don't also have to think about transcripts.
            self.add_session_transcript(cwd=effective_cwd)
        return path

    def add_session_transcript(
        self,
        *,
        cwd: str,
        dirname: str | None = None,
        session_id: str = "11111111-1111-1111-1111-111111111111",
        relocated_cwd: str | None = None,
    ) -> Path:
        directory = self.source / (dirname or hook.claude_project_dirname(cwd))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{session_id}.jsonl"
        lines = [json.dumps({"type": "attachment", "cwd": cwd})]
        if relocated_cwd is not None:
            lines.append(json.dumps({"type": "relocated", "relocatedCwd": relocated_cwd}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
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

    def test_redacts_compressed_ipv6_forms(self) -> None:
        # Independent Codex sol/xhigh review (2026-08-17,
        # CODEX-SOL-MAX-REVIEW-claude-codex-memory-bridge-2026-08-17.md,
        # P1-3): the previous hand-rolled regex only matched fully-expanded
        # IPv6 and passed every real-world compressed ("::") form through
        # unredacted. These are the review's own reproduction vectors
        # (documentation/reserved ranges, not real server addresses).
        self.fixture.add_memory(
            "# server addresses\n"
            "expanded 2001:0db8:0000:0000:0000:ff00:0042:8329\n"
            "compressed 2001:db8::1\n"
            "linklocal fe80::1234\n"
            "loopback ::1\n"
        )
        output = self.fixture.run("server addresses")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("2001:0db8:0000:0000:0000:ff00:0042:8329", context)
        self.assertNotIn("2001:db8::1", context)
        self.assertNotIn("fe80::1234", context)
        self.assertNotIn("::1", context)
        self.assertEqual(context.count("[REDACTED_IP]"), 4)

    def test_ipv6_redaction_does_not_corrupt_ordinary_code_and_prose(self) -> None:
        # Round-2 regression (independent Claude opus5/max review,
        # 2026-08-17, N2): the P1-3 IPv6 fix's candidate regex only excluded
        # hex/dot/colon neighbors, not ordinary letters, so a hex-letter run
        # embedded in an unrelated word could still parse as a syntactically
        # valid *compressed* IPv6 address (e.g. "d::" in "std::vector" is a
        # valid address: group 0x000d + "::"). That silently deleted real
        # characters from both sides of the match, not just failed to
        # redact something. These are the review's own reproduction
        # vectors.
        self.fixture.add_memory(
            "# code notes\n"
            "std::vector<int> is a C++ container.\n"
            "See Foo::bar() and namespace::fn for details.\n"
            "hello::world and df::stat are just identifiers.\n"
            "A CSS rule can start with ::before.\n"
        )
        output = self.fixture.run("code notes")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        for untouched in (
            "std::vector<int>",
            "Foo::bar()",
            "namespace::fn",
            "hello::world",
            "df::stat",
            "::before",
        ):
            self.assertIn(untouched, context)
        self.assertNotIn("[REDACTED_IP]", context)

    def test_redacts_mac_addresses(self) -> None:
        # Round-2 regression (independent Claude opus5/max review,
        # 2026-08-17, N3): ipaddress.ip_address() correctly rejects a MAC's
        # 6 groups of 2 hex digits as invalid IPv6, so switching to it for
        # P1-3 silently dropped MAC redaction that round 1's looser,
        # unvalidated regex had caught (by accident, but caught it).
        self.fixture.add_memory(
            "# device notes\n"
            "interface hwaddr de:ad:be:ef:00:11\n"
            "another form AC:DE:48:00:11:22 and 00:1B:44:11:3A:B7\n"
        )
        output = self.fixture.run("device notes")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("de:ad:be:ef:00:11", context)
        self.assertNotIn("AC:DE:48:00:11:22", context)
        self.assertNotIn("00:1B:44:11:3A:B7", context)
        self.assertEqual(context.count("[REDACTED_IP]"), 3)

    def test_skips_symlinked_memory(self) -> None:
        symlink_cwd = "/Users/tester/symlink-project"
        external = self.root_external_memory("# forbidden topic\nsecret detail")
        symlink_dir = self.fixture.source / hook.claude_project_dirname(symlink_cwd) / "memory"
        symlink_dir.mkdir(mode=0o700, parents=True)
        (symlink_dir / "MEMORY.md").symlink_to(external)
        self.fixture.add_session_transcript(cwd=symlink_cwd)
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
        # For short, BMP-only, <=200-char paths (both cases here), the
        # transform reduces to "every non-ASCII-alphanumeric character
        # becomes one '-'" -- confirmed character-for-character against this
        # session's own real ~/.claude/projects/ mapping. The NFC
        # normalization, UTF-16-code-unit semantics, and 200-char/hash-suffix
        # branch (see claude_project_dirname's docstring, disassembled
        # directly from the installed Claude Code 2.1.233 binary) only
        # change the output for non-BMP characters or longer paths -- see the
        # dedicated tests below for those.
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

    def test_claude_project_dirname_matches_non_bmp_utf16_semantics(self) -> None:
        # Independent Codex sol/xhigh review (2026-08-17,
        # CODEX-SOL-MAX-REVIEW-claude-codex-memory-bridge-2026-08-17.md,
        # P1-2) found the previous per-Unicode-code-point implementation
        # disagreed with the real Claude Code binary for non-BMP characters:
        # JS's non-`u`-flag regex replaces per UTF-16 code unit, so one
        # emoji (a surrogate pair) becomes two '-' characters, not one. This
        # exact vector -- and the real binary's exact output for it -- comes
        # from that review.
        self.assertEqual(
            hook.claude_project_dirname("/tmp/emoji-\U0001f600-x"),
            "-tmp-emoji----x",
        )

    def test_claude_project_dirname_caps_long_paths_with_hash_suffix(self) -> None:
        # Independent Codex sol/xhigh review (same report, P1-2) found the
        # real Claude Code binary truncates sanitized names longer than 200
        # characters to 200 chars + '-' + a hash of the original cwd, which
        # the previous implementation didn't replicate at all. This checks
        # the *shape* (length, cap point, hash-suffix presence) rather than
        # a specific hash value, since the review's own long-path vector
        # used a different literal path than this one.
        long_cwd = "/" + "a" * 219
        result = hook.claude_project_dirname(long_cwd)
        self.assertEqual(len(result), 207)  # 200 + '-' + 6 base-36 hash chars
        self.assertEqual(result[:200], "-" + "a" * 199)
        self.assertEqual(result[200], "-")
        self.assertRegex(result[201:], r"^[0-9a-z]{1,6}$")

    def test_cross_workspace_sanitizer_collision_fails_closed(self) -> None:
        # The most severe independent finding (Codex sol/xhigh, same report,
        # P1-1): claude_project_dirname() is not injective -- distinct real
        # cwd values can sanitize to the identical directory name (this is
        # true of the real Claude Code binary's own naming too, not just
        # this bridge's reproduction of it). Two such cwd values here
        # collide to the same sanitized name; only the first has a memory
        # file *and* a session transcript recording it as that project's
        # cwd. A request from the second (colliding, but genuinely
        # different, and never-recorded-in-any-transcript) cwd must not
        # receive the first's content -- this is the exact scenario the
        # review's own reproduction used, and the exact case
        # _session_recorded_cwd_matches exists to close.
        cwd_a = "/tmp/collision/team/app"
        cwd_b = "/tmp/collision/team-app"
        self.assertEqual(
            hook.claude_project_dirname(cwd_a), hook.claude_project_dirname(cwd_b)
        )
        self.fixture.add_memory("# victim secret\nVICTIM_PRIVATE_91c2f0", cwd=cwd_a)
        output = self.fixture.run("victim secret", cwd=cwd_b)
        self.assertEqual(output, "")

    def test_session_transcript_verification_still_allows_the_real_owner(self) -> None:
        # The positive counterpart to the collision test above: a request
        # from the cwd that genuinely IS recorded in the resolved project's
        # own transcript must still work normally.
        cwd_a = "/tmp/collision/team/app"
        self.fixture.add_memory("# victim secret\nVICTIM_PRIVATE_91c2f0", cwd=cwd_a)
        output = self.fixture.run("victim secret", cwd=cwd_a)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("VICTIM_PRIVATE_91c2f0", context)

    def test_relocated_cwd_marker_is_honored(self) -> None:
        # Mirrors the real binary's own fallback (`hJc`/`relocatedCwd`): a
        # transcript's "relocated" marker is preferred over its plain "cwd"
        # field. This only actually changes which directory gets served when
        # the relocated cwd derives to the *same* directory name as the
        # original recorded cwd (e.g. a workspace renamed between two paths
        # that happen to sanitize identically) -- this bridge does not do a
        # reverse/cross-directory lookup for a relocated project whose new
        # cwd derives to a *different* name (independent Claude opus5/max
        # review, 2026-08-17, F4: a known, documented, deliberately
        # out-of-scope limitation -- see read_memory_documents()'s comment).
        old_cwd = "/tmp/renamed/team/app"
        new_cwd = "/tmp/renamed/team-app"
        self.assertEqual(
            hook.claude_project_dirname(old_cwd), hook.claude_project_dirname(new_cwd)
        )
        self.fixture.add_memory(
            "# moved project\nsurvives relocation", cwd=old_cwd, with_transcript=False
        )
        self.fixture.add_session_transcript(
            cwd=old_cwd,
            dirname=hook.claude_project_dirname(old_cwd),
            relocated_cwd=new_cwd,
        )
        output = self.fixture.run("moved project", cwd=new_cwd)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("survives relocation", context)

    def test_fails_closed_when_no_session_transcript_records_this_cwd(self) -> None:
        # A project directory with a memory file but zero session
        # transcripts recording this cwd (any cwd) is not a realistic steady
        # state for a genuine Claude Code project -- MEMORY.md is only ever
        # written by a real session, and that session's own transcript is
        # written alongside it -- but the bridge still must fail closed
        # rather than trust the directory name alone.
        self.fixture.add_memory("# topic\ndetail", with_transcript=False)
        self.assertEqual(self.fixture.run("topic"), "")

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

    # --- opus5/max round-2 P3s (N5-N9) ------------------------------------

    def test_relocated_gate_and_value_come_from_the_same_line(self) -> None:
        # N5: the gate ("type":"relocated") and the value ("relocatedCwd")
        # must come from one shared record, not be found independently and
        # then mismatched across two different lines.
        tail = (
            json.dumps({"type": "other", "relocatedCwd": "/tmp/attackws"})
            + "\n"
            + json.dumps({"type": "relocated", "relocatedCwd": "/tmp/legitws"})
        )
        self.assertEqual(hook._find_relocated_cwd(tail), "/tmp/legitws")

    def test_transcript_scan_prefers_newest_and_scans_more_than_eight(self) -> None:
        # N6: scanning was capped at 8 transcripts sorted by (arbitrary
        # UUID) filename, so a legitimate owner's own transcript could sort
        # after the cap purely by chance and be refused. 20 decoys with old
        # mtimes and names that sort before the real owner's; the real
        # owner's transcript is the most recently written.
        cwd = "/tmp/many-transcripts-owner"
        dirname = hook.claude_project_dirname(cwd)
        project_dir = self.fixture.source / dirname
        project_dir.mkdir(mode=0o700, parents=True)
        old_time = 1_700_000_000.0
        for index in range(20):
            decoy = project_dir / f"aaa-decoy-{index:02d}.jsonl"
            decoy.write_text(json.dumps({"type": "attachment", "cwd": "/tmp/decoy"}) + "\n")
            decoy.chmod(0o600)
            os.utime(decoy, (old_time + index, old_time + index))
        self.fixture.add_session_transcript(cwd=cwd, dirname=dirname, session_id="zzz-owner")
        self.assertTrue(hook._session_recorded_cwd_matches(project_dir, cwd))

    def test_jsonl_line_scan_matches_js_newline_only_splitting(self) -> None:
        # N7: Python's str.splitlines() breaks on more characters (U+2028,
        # U+2029, \v, \f, ...) than JS's plain '\n' scanning does. A record
        # whose string value happens to contain one of those must still be
        # treated as a single JSONL line.
        text = json.dumps({"type": "attachment", "cwd": "/tmp/x y"})
        self.assertEqual(len(hook._jsonl_lines(text)), 1)
        self.assertEqual(
            hook._find_json_field(text, hook._CWD_FIELD_RE, "cwd", forward=True),
            "/tmp/x y",
        )

    def test_ipv6_candidate_regex_is_not_quadratic(self) -> None:
        # N8: unbounded quantifiers on both sides of the mandatory ':' made
        # matching quadratic in long uniform hex/colon runs (measured by
        # independent review: 127 KB -> 11.4s with the unbounded pattern).
        # This must stay fast regardless of any caller-side length cap.
        adversarial = "a1:" * 40_000  # 120,000 characters
        started = time.monotonic()
        hook._IPV6_CANDIDATE_RE.findall(adversarial)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_ipv6_zone_id_does_not_survive_redaction(self) -> None:
        # N9: a link-local address's zone/scope id (interface name) leaked
        # through redaction unchanged -- "[REDACTED_IP]%eth0".
        self.fixture.add_memory("# interfaces\nlistening on fe80::1%eth0 for discovery\n")
        output = self.fixture.run("interfaces")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("eth0", context)
        self.assertNotIn("fe80::1", context)
        self.assertIn("[REDACTED_IP]", context)

    # --- found by an independent post-fix verification Workflow ----------

    def test_addresses_redact_even_with_no_space_before_a_sentence_period(self) -> None:
        # Pre-existing since the very first commit, unchanged by every prior
        # fix round: _IPV4_RE and _IPV6_CANDIDATE_RE's trailing negative
        # lookaheads both disqualified a following '.', so an address
        # written as ordinary prose ("reachable at 10.0.0.1.") never
        # redacted at all -- there was no position where "next char is
        # neither digit/dot nor absent" held when a period was glued
        # directly onto the address with no separating space. No existing
        # test caught this because every prior IP-redaction fixture happened
        # to follow the address with a space or comma, never a bare period.
        self.fixture.add_memory(
            "# hosts\n"
            "reachable at 198.51.100.42.\n"
            "server at 2001:db8::1.\n"
            "backup at fe80::1234.\n"
        )
        output = self.fixture.run("hosts")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("198.51.100.42", context)
        self.assertNotIn("2001:db8::1", context)
        self.assertNotIn("fe80::1234", context)
        self.assertEqual(context.count("[REDACTED_IP]"), 3)

    def test_ipv6_zone_id_with_non_alnum_characters_still_redacts(self) -> None:
        # The first zone-id fix (N9) only accepted a plain-alnum zone/scope
        # id in its optional suffix, and -- critically -- made the *base
        # address's own* match conditional on the zone group either being
        # absent or ending cleanly. A real zone id containing anything else
        # (a VLAN suffix like "eth0.100", an underscored adapter name, a
        # Windows GUID zone id in braces) made every match boundary fail, so
        # the match failed to start at all: the base IPv6 address leaked
        # completely unredacted -- worse than before the N9 fix existed.
        self.fixture.add_memory(
            "# interfaces\n"
            "vlan sub-interface fe80::1%eth0.100\n"
            "underscored adapter fe80::1%eth_0\n"
            "windows zone fe80::1%{4D36E972-E325-11CE-BFC1-08002BE10318}\n"
        )
        output = self.fixture.run("interfaces")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        for leaked_fragment in ("fe80::1", "eth0.100", "eth_0", "4D36E972"):
            self.assertNotIn(leaked_fragment, context)
        self.assertEqual(context.count("[REDACTED_IP]"), 3)

    def test_unknown_workspace_yields_no_context_without_error(self) -> None:
        # A cwd with no matching Claude project directory at all (e.g. a
        # brand-new workspace with no Claude history yet) is an expected,
        # benign steady state, not a bridge failure -- it must not raise.
        self.fixture.add_memory("# topic\ndetail")
        output = self.fixture.run("topic", cwd="/Users/tester/never-seen-workspace")
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
