from __future__ import annotations

import hashlib
import inspect
import json
import os
import plistlib
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        # sessionId must match the filename -- real Claude Code transcripts
        # always agree, and claude_memory_hook.py now requires that
        # agreement before trusting any record in the file (independent
        # finding, 2026-08-17: forging a bare {"cwd": ...} line with no
        # sessionId used to be enough to defeat the collision defense).
        lines = [json.dumps({"type": "attachment", "cwd": cwd, "sessionId": session_id})]
        if relocated_cwd is not None:
            lines.append(
                json.dumps({"type": "relocated", "relocatedCwd": relocated_cwd, "sessionId": session_id})
            )
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

    # --- found by an independent full-audit Workflow ("完整检查") ---------

    def test_redacts_json_quoted_and_snake_case_credentials(self) -> None:
        # P0 (both root causes of the same class): _ASSIGNMENT_RE required
        # the keyword to be immediately followed by whitespace+':'/'=' (so
        # a JSON-quoted key like "password": "..." never matched -- the
        # closing quote sat where the separator needed to be) and anchored
        # the keyword with \b on both sides (so "token" embedded in
        # "access_token" or "GITHUB_TOKEN" never matched either, since '_'
        # is itself a word character and no boundary exists there). Real
        # secrets pasted as JSON, or under the dominant snake_case/
        # SCREAMING_SNAKE_CASE naming convention, leaked completely.
        self.fixture.add_memory(
            "# credentials\n"
            '{"password": "hunter2xyz", "api_key": "plainsecretvalue123"}\n'
            "access_token=abcdef123456\n"
            "DATABASE_PASSWORD=supersecretvalue\n"
            "AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxx\n"
        )
        output = self.fixture.run("credentials")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        for leaked in (
            "hunter2xyz",
            "plainsecretvalue123",
            "abcdef123456",
            "supersecretvalue",
            "xxxxxxxxxxxxxxxxxxxx",
        ):
            self.assertNotIn(leaked, context)
        self.assertIn("[REDACTED]", context)

    def test_redacts_email_immediately_adjacent_to_cjk_text(self) -> None:
        # Offline dogfood scan finding, 2026-08-20: `_EMAIL_RE` anchored both
        # ends with plain `\b`, which is Unicode-aware by default -- Python's
        # `\w` treats CJK ideographs as word characters, so an email glued
        # directly onto CJK text with no separating whitespace has no
        # word/non-word transition at either edge and the boundary check
        # silently fails, leaking the address in full. Same class of bug
        # already fixed for `_CURRENT_TASK_PLAN_RE`/`_AFFIRMATION_RE` in
        # write_candidate_capture.py. Uses a synthetic email/domain, not the
        # real one found in the scan.
        leading_and_trailing = hook.redact("资料user@example.com学习")
        self.assertNotIn("user@example.com", leading_and_trailing)
        self.assertIn("[REDACTED_EMAIL]", leading_and_trailing)

        leading_only = hook.redact("开头无空格user@example.com")
        self.assertNotIn("user@example.com", leading_only)
        self.assertIn("[REDACTED_EMAIL]", leading_only)

        trailing_only = hook.redact("user@example.com结尾无空格")
        self.assertNotIn("user@example.com", trailing_only)
        self.assertIn("[REDACTED_EMAIL]", trailing_only)

        # Sanity: ordinary ASCII-adjacent emails still redact exactly as
        # before (the fix must not change any pure-ASCII boundary decision).
        ascii_case = hook.redact("contact me at a.b+c@example.co.uk please")
        self.assertNotIn("a.b+c@example.co.uk", ascii_case)
        self.assertIn("[REDACTED_EMAIL]", ascii_case)

    def test_email_redaction_still_catches_unicode_homoglyph_after_ascii_retrofit(
        self,
    ) -> None:
        # Independent dual review, 2026-08-20: the CJK-adjacency fix above was first shipped as
        # `re.ASCII` on `_EMAIL_RE`'s compile flags. `re.ASCII` closes the CJK gap, but it also
        # narrows `IGNORECASE`'s casefolding to ASCII-only -- a real regression the review caught.
        # U+212A KELVIN SIGN ("K") case-folds to ASCII "k" under Python's default Unicode
        # casefolding (confirmed empirically) but NOT under `re.ASCII`'s restricted table, so a
        # homoglyph domain using U+212A in place of an ASCII "K" matched and redacted under plain
        # `IGNORECASE` before the CJK fix (an accidental but real property of Unicode casefolding)
        # and stopped matching once `re.ASCII` was added. `_EMAIL_RE` is now retrofitted to the
        # same explicit-lookaround idiom as `_LONG_BLOB_RE`/`_MAC_ADDRESS_RE`/`_ASSIGNMENT_RE`
        # instead of `re.ASCII`, so `IGNORECASE` regains full Unicode casefolding while the
        # CJK-adjacency fix (confirmed by the tests above) is unaffected.
        kelvin = "K"
        self.assertEqual(__import__("unicodedata").name(kelvin), "KELVIN SIGN")

        tld_homoglyph = hook.redact(f"visit user@example.u{kelvin} today")
        self.assertNotIn(f"user@example.u{kelvin}", tld_homoglyph)
        self.assertIn("[REDACTED_EMAIL]", tld_homoglyph)

        domain_start_homoglyph = hook.redact(f"visit user@e{kelvin}ample.com today")
        self.assertNotIn(f"user@e{kelvin}ample.com", domain_start_homoglyph)
        self.assertIn("[REDACTED_EMAIL]", domain_start_homoglyph)

    def test_redacts_bearer_token_immediately_adjacent_to_cjk_text(self) -> None:
        # Independent dual review, 2026-08-20: `_BEARER_RE` anchored its leading edge with plain
        # `\b`, the same CJK-adjacency gap fixed for `_EMAIL_RE` above -- a Bearer token glued
        # directly onto CJK text with no separating whitespace never matched a boundary before
        # "Bearer", leaking the token in full. Synthetic token below.
        leading = hook.redact("授权Bearer abcdefgh12345678令牌")
        self.assertNotIn("abcdefgh12345678", leading)
        self.assertIn("[REDACTED]", leading)

        # Sanity: ordinary ASCII-adjacent Bearer tokens still redact exactly as before.
        ascii_case = hook.redact("Authorization: Bearer abcdefgh12345678 sent")
        self.assertNotIn("abcdefgh12345678", ascii_case)
        self.assertIn("[REDACTED]", ascii_case)

    def test_bearer_token_redaction_still_catches_ignorecase_homoglyph_adjacency(self) -> None:
        # Exhaustive-audit finding (independent dual review, 2026-08-20, P2-1): the CJK-adjacency
        # fix above (`(?<![A-Za-z0-9_])` in place of `\b`) has a narrower bug of its own --
        # `_BEARER_RE` also carries `(?i)`, and IGNORECASE applies to every character class in the
        # compiled pattern, including the lookbehind's, not just the literal "Bearer" text it was
        # meant for. Four non-ASCII code points case-fold to an ASCII letter under Python's default
        # Unicode IGNORECASE table (confirmed empirically, not assumed): U+0130, U+0131, U+017F,
        # and U+212A. A Bearer token directly preceded by one of these (no separating space)
        # previously read as "preceded by a word character", the lookbehind failed, and the whole
        # token leaked in full. Fixed by scoping IGNORECASE back off just for the lookbehind's
        # character class: `(?<!(?-i:[A-Za-z0-9_]))`, with the pattern's global `(?i)` restored (and
        # kept) so the token-body class keeps its normal case-insensitive matching.
        #
        # CORRECTED, 2026-08-20 (this round): a prior round's comment here claimed the
        # `(?<!(?-i:[A-Za-z0-9_]))` idiom "does not work" in this interpreter and instead shipped a
        # fix that dropped the pattern's global `(?i)` and scoped `(?i:...)` only around the literal
        # "Bearer" text. That claim was false -- two independent, from-scratch reviews (Claude opus +
        # Codex) each confirmed the idiom works correctly on `/usr/bin/python3` 3.9.6 -- and the
        # dropped-global-`(?i)` workaround itself introduced a real regression: a token value
        # containing one of the four homoglyphs (e.g. `Bearer abc<I_DOT_ABOVE>hijklmn`, tested below) lost the
        # class's incidental taint-match once the global flag was gone, so `{8,}` could fail to be
        # satisfied and the whole match silently failed to start, leaking the token completely
        # unredacted. See `claude_memory_hook.py`'s comment above `_BEARER_RE` for the full account,
        # including the likely cause of the prior round's false test result.
        homoglyphs = {
            "\u0130": "LATIN CAPITAL LETTER I WITH DOT ABOVE",
            "\u0131": "LATIN SMALL LETTER DOTLESS I",
            "\u017f": "LATIN SMALL LETTER LONG S",
            "\u212a": "KELVIN SIGN",
        }
        unicodedata = __import__("unicodedata")
        for char, expected_name in homoglyphs.items():
            self.assertEqual(unicodedata.name(char), expected_name)
            redacted = hook.redact(f"{char}Bearer abcdefgh12345678")
            self.assertNotIn("abcdefgh12345678", redacted, expected_name)
            self.assertIn("[REDACTED]", redacted, expected_name)

        # Sanity: a genuine ASCII letter glued directly onto "Bearer" (no space) must still NOT
        # be treated as a boundary -- deliberate, pre-existing behavior, not a regression.
        self.assertIsNone(hook._BEARER_RE.search("xBearer abcdefgh12345678"))
        # Sanity: case-insensitive matching of the literal keyword itself is unaffected.
        self.assertIn("[REDACTED]", hook.redact("Authorization: BEARER abcdefgh12345678"))
        self.assertIn("[REDACTED]", hook.redact("authorization: bEaReR abcdefgh12345678"))

    def test_bearer_token_redaction_catches_homoglyph_inside_the_token_value_itself(self) -> None:
        # Regression this round (found independently by the opus-side review, 2026-08-20): the
        # prior round's mistaken "drop global (?i)" workaround for the P2-1 boundary bug (see the
        # test above) broke a second, different case -- one of the four IGNORECASE-tainted
        # homoglyphs appearing INSIDE the token value itself, not at the boundary. With no global
        # `(?i)`, the token-body class `[A-Za-z0-9._~+/=-]{8,}` no longer taint-matches the
        # homoglyph, so if fewer than 8 plain-ASCII characters precede it the `{8,}` quantifier is
        # never satisfied and the *entire* match -- "Bearer " and the whole token -- silently fails
        # to start, leaking the token completely unredacted (not a partial redaction). The corrected
        # fix (global `(?i)` restored, only the boundary lookaround locally negated) closes this:
        # the token-body class regains its taint-match and swallows the homoglyph as part of the
        # token, redacting the whole thing.
        homoglyphs = {
            "\u0130": "LATIN CAPITAL LETTER I WITH DOT ABOVE",
            "\u0131": "LATIN SMALL LETTER DOTLESS I",
            "\u017f": "LATIN SMALL LETTER LONG S",
            "\u212a": "KELVIN SIGN",
        }
        unicodedata = __import__("unicodedata")
        for char, expected_name in homoglyphs.items():
            self.assertEqual(unicodedata.name(char), expected_name)
            token_value = f"abc{char}hijklmn"
            redacted = hook.redact(f"Bearer {token_value}")
            self.assertNotIn(token_value, redacted, expected_name)
            self.assertIn("[REDACTED]", redacted, expected_name)

    def test_redacts_prefixed_token_immediately_adjacent_to_cjk_text(self) -> None:
        # Independent dual review, 2026-08-20: `_TOKEN_RE` (sk-/ghp_/AKIA-style prefixed tokens)
        # anchored both ends with plain `\b` -- the same CJK-adjacency gap. Both a leading CJK
        # run before the prefix and a trailing CJK run right after the token body previously
        # leaked the whole token unredacted. Synthetic tokens below.
        leading = hook.redact("密钥sk-proj-abcdefgh12345678泄露")
        self.assertNotIn("sk-proj-abcdefgh12345678", leading)
        self.assertIn("[REDACTED_TOKEN]", leading)

        trailing = hook.redact("sk-proj-abcdefgh12345678泄露了")
        self.assertNotIn("sk-proj-abcdefgh12345678", trailing)
        self.assertIn("[REDACTED_TOKEN]", trailing)

        akia_trailing = hook.redact("AKIAABCDEFGHIJKLMNOP泄露了")
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", akia_trailing)
        self.assertIn("[REDACTED_TOKEN]", akia_trailing)

        # Sanity: ordinary ASCII-adjacent prefixed tokens still redact exactly as before.
        ascii_case = hook.redact("key is sk-proj-abcdefgh12345678 today")
        self.assertNotIn("sk-proj-abcdefgh12345678", ascii_case)
        self.assertIn("[REDACTED_TOKEN]", ascii_case)

    def test_redacts_standalone_jwt_immediately_adjacent_to_cjk_text(self) -> None:
        # Independent dual review, 2026-08-20: `_JWT_RE` anchored both ends with plain `\b` --
        # the same CJK-adjacency gap. A standalone JWT (no "Bearer " prefix) glued directly onto
        # CJK text on either side previously leaked in full.
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        leading = hook.redact(f"令牌{jwt}泄露")
        self.assertNotIn(jwt, leading)
        self.assertIn("[REDACTED_TOKEN]", leading)

        trailing = hook.redact(f"{jwt}泄露了")
        self.assertNotIn(jwt, trailing)
        self.assertIn("[REDACTED_TOKEN]", trailing)

        # Sanity: ordinary ASCII-adjacent standalone JWTs still redact exactly as before.
        ascii_case = hook.redact(f"response came back as {jwt} today")
        self.assertNotIn(jwt, ascii_case)
        self.assertIn("[REDACTED_TOKEN]", ascii_case)

    def test_redacts_url_userinfo_immediately_adjacent_to_cjk_text(self) -> None:
        # Independent dual review, 2026-08-20: `_URL_USERINFO_RE` anchored its leading edge
        # (before the URL scheme) with plain `\b` -- the same CJK-adjacency gap. The review noted
        # the password component previously "survived only because `_EMAIL_RE` incidentally
        # caught the remainder, a backstop that vanishes with a dotless host" -- this uses a
        # dotless host so nothing else can catch the leak if `_URL_USERINFO_RE` itself fails.
        leading = hook.redact("访问https://user:supersecretpw@host今天")
        self.assertNotIn("supersecretpw", leading)
        self.assertIn("[REDACTED]", leading)

        # Sanity: ordinary ASCII-adjacent userinfo URLs still redact exactly as before.
        ascii_case = hook.redact("visit https://user:supersecretpw@host.example.com today")
        self.assertNotIn("supersecretpw", ascii_case)
        self.assertIn("[REDACTED]", ascii_case)

    def test_url_userinfo_and_email_confirmed_immune_to_ignorecase_homoglyph_taint(self) -> None:
        # Exhaustive-audit finding (independent dual review, 2026-08-20): `_URL_USERINFO_RE` and
        # `_EMAIL_RE` share the exact same `(?i)` + ASCII-lookaround shape that made `_BEARER_RE`
        # leak (see test above) and `_ASSIGNMENT_RE` leak (see test below) on the four
        # IGNORECASE-tainted homoglyphs U+0130, U+0131, U+017F, and U+212A -- but the review that
        # found the `_BEARER_RE`/`_ASSIGNMENT_RE` bugs speculated these two were "accidentally
        # immune" rather than asserting it outright. Verified here, not assumed: for exactly these
        # four vectors, both patterns' own leading content-matching character class (`[a-z]` for the
        # URL scheme, `[A-Z0-9._%+-]` for the email local-part) is *itself* IGNORECASE-tainted the
        # same way, so a homoglyph directly adjacent to the credential is simply absorbed into the
        # match as an extra character of that open class, rather than needing the boundary
        # lookaround to succeed at all.
        #
        # NOTE, 2026-08-20 (this round): "immune" does NOT generalize to every position -- a
        # different vector (independent Codex review) defeats `_EMAIL_RE`'s absorption mechanism by
        # placing the homoglyph where the greedy TLD class's own backtracking runs out of positions
        # to retry (`user@example.com` + homoglyph + `_`, see the dedicated test below). Both
        # patterns are now fixed with the same explicitly-scoped `(?-i:...)` idiom as `_BEARER_RE`/
        # `_ASSIGNMENT_RE` regardless, so nothing here still depends on the absorption coincidence;
        # this test remains as regression coverage for the vectors it always covered.
        unicodedata = __import__("unicodedata")
        homoglyphs = {
            "\u0130": "LATIN CAPITAL LETTER I WITH DOT ABOVE",
            "\u0131": "LATIN SMALL LETTER DOTLESS I",
            "\u017f": "LATIN SMALL LETTER LONG S",
            "\u212a": "KELVIN SIGN",
        }
        for char, expected_name in homoglyphs.items():
            self.assertEqual(unicodedata.name(char), expected_name)

            leading_url = hook.redact(f"see {char}https://user:supersecretpw@host.example.com/x")
            self.assertNotIn("supersecretpw", leading_url, expected_name)

            trailing_url = hook.redact(f"see https://user:supersecretpw{char}@host.example.com/x")
            self.assertNotIn("supersecretpw", trailing_url, expected_name)

            leading_email = hook.redact(f"contact {char}user@example.com now")
            self.assertNotIn("user@example.com", leading_email, expected_name)

            trailing_email = hook.redact(f"contact user@example.com{char} now")
            self.assertNotIn("user@example.com", trailing_email, expected_name)

    def test_email_redaction_catches_homoglyph_directly_followed_by_a_word_character(self) -> None:
        # Regression this round (found independently by the Codex-side review, 2026-08-20): the
        # test above puts a homoglyph right after the TLD followed by a *space* -- there,
        # `_EMAIL_RE`'s greedy `[A-Z]{2,}` TLD class absorbs the homoglyph (it taint-matches under
        # `(?i)`) and the trailing lookaround then sees the space and succeeds, same as the "immune"
        # mechanism above. But when a word character immediately follows the homoglyph instead of a
        # space (e.g. `user@example.com` + homoglyph + `_`), that absorption stops working: the
        # greedy match first tries including the homoglyph in the TLD, and the trailing lookaround
        # fails (next char is `_`, a word character); backtracking off the homoglyph tries again
        # right after ".com", but the lookaround there *also* fails because the unscoped
        # `(?![A-Za-z0-9_])` lookahead was itself IGNORECASE-tainted and read the homoglyph as a
        # word character too -- so every possible match boundary fails and the whole address leaks
        # unredacted, not just the homoglyph-adjacent part. `_URL_USERINFO_RE` has no such
        # "boundary two positions in a row" hazard on its leading edge, but is checked here too
        # since the underlying idiom is now the same for both patterns.
        unicodedata = __import__("unicodedata")
        homoglyphs = {
            "\u0130": "LATIN CAPITAL LETTER I WITH DOT ABOVE",
            "\u0131": "LATIN SMALL LETTER DOTLESS I",
            "\u017f": "LATIN SMALL LETTER LONG S",
            "\u212a": "KELVIN SIGN",
        }
        for char, expected_name in homoglyphs.items():
            self.assertEqual(unicodedata.name(char), expected_name)

            # Codex's exact repro shape: TLD directly followed by homoglyph directly followed by '_'.
            trailing_email_then_word = hook.redact(f"user@example.com{char}_")
            self.assertNotIn("user@example.com", trailing_email_then_word, expected_name)
            self.assertIn("[REDACTED_EMAIL]", trailing_email_then_word, expected_name)

            # Codex's exact repro shape for the URL password: homoglyph directly before the scheme,
            # dotless host so nothing else can backstop a failure to redact.
            leading_url = hook.redact(f"{char}https://user:supersecretpw@host")
            self.assertNotIn("supersecretpw", leading_url, expected_name)
            self.assertIn("[REDACTED]", leading_url, expected_name)

    def test_redacts_generic_service_prefixed_key_assignment(self) -> None:
        # Offline dogfood scan finding, 2026-08-20: `_ASSIGNMENT_RE` only
        # recognized `key` as part of two specifically-named compounds,
        # `api_key`/`private_key` -- a service-prefixed key variable whose
        # name isn't one of those two (e.g. `soga_key=...`) matched no
        # alternative at all and leaked completely unredacted. Fixed by
        # adding a generic `<word>_key`/`<word>-key` alternative, the same
        # way `token=`/`secret=` already match generically. Synthetic value
        # below, not the real one found in the scan.
        text = "soga_key=abcd1234efgh5678ijkl9012mnop3456"
        redacted = hook.redact(text)
        self.assertNotIn("abcd1234efgh5678ijkl9012mnop3456", redacted)
        self.assertIn("soga_key=[REDACTED]", redacted)

        # False-positive guard: "turkey"/"monkey" contain the letters "key"
        # with no separator before them and must NOT be treated as a
        # compound `*_key` assignment.
        self.assertEqual(hook.redact("turkey=5"), "turkey=5")
        self.assertEqual(hook.redact("monkey=xyz123"), "monkey=xyz123")

        # Existing named compounds and other generic keywords still redact
        # exactly as before.
        still_works = hook.redact(
            '{"password": "hunter2xyz", "api_key": "plainsecretvalue123"}\n'
            "access_token=abcdef123456\n"
            "AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxx\n"
        )
        for leaked in (
            "hunter2xyz",
            "plainsecretvalue123",
            "abcdef123456",
            "xxxxxxxxxxxxxxxxxxxx",
        ):
            self.assertNotIn(leaked, still_works)

    def test_assignment_redaction_still_catches_ignorecase_homoglyph_adjacency(self) -> None:
        # Exhaustive-audit finding (independent dual review, 2026-08-20): `_ASSIGNMENT_RE` carries
        # the identical `(?i)` + ASCII-lookbehind combination as `_BEARER_RE` (see the test above)
        # -- but this pattern was not one the review named; found only by auditing every compiled
        # pattern in this file rather than trusting the review's own named list. Confirmed
        # empirically: a keyword directly preceded (no separator) by one of the same four
        # IGNORECASE-tainted homoglyphs (U+0130, U+0131, U+017F, U+212A) failed the lookbehind and
        # leaked the assigned value in full, e.g. "İpassword=hunter2value" never redacted.
        # Fixed the same way as `_BEARER_RE`: global `(?i)` restored, with only the boundary
        # lookbehind locally negated (`(?<!(?-i:[A-Za-z0-9]))`) -- see that pattern's comment in
        # `claude_memory_hook.py` for the corrected account of why this idiom works (a prior round
        # wrongly concluded it didn't) and why the interim "drop global `(?i)`" workaround this
        # pattern briefly carried was itself a regression (see the content-side test below).
        homoglyphs = {
            "\u0130": "LATIN CAPITAL LETTER I WITH DOT ABOVE",
            "\u0131": "LATIN SMALL LETTER DOTLESS I",
            "\u017f": "LATIN SMALL LETTER LONG S",
            "\u212a": "KELVIN SIGN",
        }
        unicodedata = __import__("unicodedata")
        for char, expected_name in homoglyphs.items():
            self.assertEqual(unicodedata.name(char), expected_name)
            redacted = hook.redact(f"{char}password=hunter2value")
            self.assertNotIn("hunter2value", redacted, expected_name)
            self.assertIn("[REDACTED]", redacted, expected_name)

        # Sanity: a genuine ASCII letter glued directly onto the keyword (no separator) must
        # still NOT be treated as a boundary -- deliberate, pre-existing behavior.
        self.assertEqual(hook.redact("xpassword=hunter2value"), "xpassword=hunter2value")
        # Sanity: case-insensitive matching of the literal keyword itself is unaffected.
        self.assertIn("[REDACTED]", hook.redact("PASSWORD=hunter2value"))
        self.assertIn("[REDACTED]", hook.redact("soga_KEY=abcd1234efgh5678ijkl9012mnop3456"))

    def test_assignment_redaction_catches_homoglyph_inside_the_keyword_suffix(self) -> None:
        # Regression this round (found independently by the opus-side review, 2026-08-20): the
        # prior round's mistaken "drop global (?i)" workaround for the boundary bug above broke a
        # second, different case -- one of the four IGNORECASE-tainted homoglyphs appearing INSIDE
        # a `<word>_key`/`<word>-key`-style compound suffix, not at the boundary. With no global
        # `(?i)`, `(?:[_-][A-Za-z0-9]+)*` no longer taint-matches the homoglyph, so it cannot be
        # consumed as part of the keyword-suffix run; the mandatory separator (`sep`) then never
        # lines up at the position right after, and the *entire* assignment -- keyword and value
        # both -- silently fails to match, leaking the value completely unredacted (not a partial
        # redaction). The corrected fix (global `(?i)` restored, only the boundary lookbehind
        # locally negated) closes this: the suffix class regains its taint-match and absorbs the
        # homoglyph as part of the keyword run, redacting the whole assignment.
        homoglyphs = {
            "\u0130": "LATIN CAPITAL LETTER I WITH DOT ABOVE",
            "\u0131": "LATIN SMALL LETTER DOTLESS I",
            "\u017f": "LATIN SMALL LETTER LONG S",
            "\u212a": "KELVIN SIGN",
        }
        unicodedata = __import__("unicodedata")
        for char, expected_name in homoglyphs.items():
            self.assertEqual(unicodedata.name(char), expected_name)
            text = f"password_b{char}lg{char}=hunter2value"
            redacted = hook.redact(text)
            self.assertNotIn("hunter2value", redacted, expected_name)
            self.assertIn("[REDACTED]", redacted, expected_name)

    def test_redacts_standalone_jwt_with_no_prefix(self) -> None:
        # P1: a JWT with no "Bearer " prefix and no recognized key=/key:
        # context (e.g. pasted from a log line or curl output) leaked
        # completely -- dots split it into three pieces each too short for
        # the long-blob fallback, and it has no fixed prefix _TOKEN_RE knew
        # about.
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        self.fixture.add_memory(f"# auth log\nresponse came back as {jwt} today\n")
        output = self.fixture.run("auth log")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn(jwt, context)
        self.assertIn("[REDACTED_TOKEN]", context)

    def test_pem_private_key_redacted_even_when_body_exceeds_4000_chars(self) -> None:
        # P0: split_blocks() truncated block text to 4,000 characters
        # *before* redact() ever ran on it (redact() was only called later,
        # in build_context(), on the already-truncated text). _PEM_RE needs
        # to see both the BEGIN and END markers in the same string to match
        # at all; any real key whose body exceeds 4,000 characters (common
        # -- a realistic RSA key wrapped at ordinary line widths easily
        # does) had its END marker silently truncated away first, so the
        # regex never matched and the key's body leaked almost entirely in
        # the clear. Fixed by redacting the full, untruncated block text
        # before truncating it.
        key_body = "".join(f"{i:08x}" for i in range(500))  # deterministic, >4000 chars
        wrapped = "\n".join(key_body[i : i + 40] for i in range(0, len(key_body), 40))
        pem = f"-----BEGIN RSA PRIVATE KEY-----\n{wrapped}\n-----END RSA PRIVATE KEY-----"
        self.assertGreater(len(pem), 4_000)
        self.fixture.add_memory(f"# key\n{pem}\n")
        output = self.fixture.run("key")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("BEGIN RSA PRIVATE KEY", context)
        # None of the key body's 40-char lines should survive verbatim.
        for line in wrapped.split("\n"):
            self.assertNotIn(line, context)
        self.assertIn("REDACTED_PRIVATE_KEY", context)

    def test_assignment_redaction_does_not_swallow_adjacent_query_params(self) -> None:
        # P2: the value character class didn't exclude '&', so a recognized
        # keyword's value directly followed by more '&key=value' pairs (a
        # pasted curl command or URL) had all of them swallowed into the
        # redacted span and deleted, not just the one secret value.
        self.fixture.add_memory(
            "# curl command\ncurl https://x.example.com?token=abc123&next=xyz&other=1\n"
        )
        output = self.fixture.run("curl command")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("abc123", context)
        self.assertIn("next=xyz", context)
        self.assertIn("other=1", context)

    # --- dogfood dual-review finding, 2026-08-22 (independent Claude opus + Codex, same
    # 130-item real-world sample, both agreeing on which items): `_ASSIGNMENT_RE` only
    # recognizes ASCII assignment syntax and an ASCII-only keyword vocabulary, so a
    # Chinese-labeled secret never even reached its separator check. All values below are
    # synthetic, constructed for these tests -- never the real secrets the review found. ---

    def test_redacts_cjk_label_with_fullwidth_colon_in_prose(self) -> None:
        # Gap (a): a full-width Chinese colon "：" (U+FF1A) used as a label
        # separator inside flowing prose, with no whitespace, embedded
        # mid-sentence -- not a standalone config line.
        unicodedata = __import__("unicodedata")
        self.assertEqual(unicodedata.name("："), "FULLWIDTH COLON")
        text = "联系客服获取API密码：Xk9$mQ2vR8pL用于登录"
        redacted = hook.redact(text)
        self.assertNotIn("Xk9$mQ2vR8pL", redacted)
        self.assertIn("[REDACTED]", redacted)
        # The surrounding prose, including the label and the fullwidth colon
        # itself, must survive untouched -- only the value is redacted.
        self.assertIn("联系客服获取API密码：", redacted)
        self.assertIn("用于登录", redacted)

    def test_redacts_cjk_label_in_markdown_table_cell(self) -> None:
        # Gap (b): a markdown table cell pair -- label and value in separate
        # pipe-delimited cells, never joined by "="/":" the way
        # `_ASSIGNMENT_RE` expects.
        redacted = hook.redact("| 员工登录密码 | Xy9!aBcDeF12 |")
        self.assertNotIn("Xy9!aBcDeF12", redacted)
        self.assertEqual(redacted, "| 员工登录密码 | [REDACTED] |")

        # Bare single-word label cell, not just a longer compound.
        redacted_bare = hook.redact("| 密码 | SecretValue123 |")
        self.assertNotIn("SecretValue123", redacted_bare)
        self.assertEqual(redacted_bare, "| 密码 | [REDACTED] |")

    def test_redacts_cjk_inline_prose_mention(self) -> None:
        # Gap (c): an inline prose mention where the secret value follows a
        # label word directly inside a sentence, with a Chinese verb
        # ("是"), no separator at all, or plain whitespace.
        verb = hook.redact("root密码是Zq7#Lm3nP9wT")
        self.assertNotIn("Zq7#Lm3nP9wT", verb)
        self.assertEqual(verb, "root密码是[REDACTED]")

        space = hook.redact("密码 Fh2$Nb8xR5vK 就是这个")
        self.assertNotIn("Fh2$Nb8xR5vK", space)
        self.assertEqual(space, "密码 [REDACTED] 就是这个")

        # A short-lived OAuth-style device code, a different keyword
        # ("授权码") from the same new vocabulary.
        device_code = hook.redact("授权码是WDJB-MJHT")
        self.assertNotIn("WDJB-MJHT", device_code)
        self.assertEqual(device_code, "授权码是[REDACTED]")

    def test_cjk_secret_redaction_end_to_end_through_run(self) -> None:
        # Same three gap forms, exercised through the full hook.run() pipeline
        # (memory file -> split_blocks -> redact -> emitted context), not just
        # the bare redact() unit calls above.
        self.fixture.add_memory(
            "# 生产环境凭据\n"
            "员工登录密码：Xk9$mQ2vR8pL\n"
            "| 字段 | 值 |\n"
            "| 员工登录密码 | Xy9!aBcDeF12 |\n"
            "root密码是Zq7#Lm3nP9wT\n"
        )
        output = self.fixture.run("生产环境凭据")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        for leaked in ("Xk9$mQ2vR8pL", "Xy9!aBcDeF12", "Zq7#Lm3nP9wT"):
            self.assertNotIn(leaked, context)
        self.assertIn("[REDACTED]", context)

    def test_cjk_secret_redaction_does_not_flag_ordinary_chinese_text(self) -> None:
        # False-positive guard: the same label words, with no secret-shaped
        # value nearby, must pass through completely unchanged.
        unchanged_cases = (
            "密码：请联系管理员重置",
            "| 密码 | 已重置，请联系管理员 |",
            "| 姓名 | 部门 |",
            "这门课讲的是密码学基础知识",
            "密码长度至少8位数字",
            "请设置您的密码",
            "密码 是一个很重要的东西",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    # --- dogfood dual-review round 2 (independent Claude opus + Codex, 2026-08-22): findings
    # against the round-1 patterns above. Every synthetic value below is newly constructed for
    # these tests, never the real secrets either review round found. Each test in this section
    # was verified to FAIL against the pre-round-2 `claude_memory_hook.py` (temporarily reverted
    # locally) and PASS after the round-2 fix, per the retry instructions. ---

    def test_cjk_secret_redaction_does_not_narrow_an_adjacent_mac_or_ipv6_match(self) -> None:
        # Round-2 P1: the round-1 CJK-secret passes ran *before* `_MAC_ADDRESS_RE` in redact()'s
        # pipeline. Their value class excludes ':', so a keyword glued directly (no separator) to
        # a longer ASCII run that was itself a MAC address truncated the ASCII prefix off that
        # run and left too few hex groups for `_MAC_ADDRESS_RE` to recognize afterward -- a
        # strict regression from "fully redacted" to "5 of 6 octets leak in the clear". Fixed by
        # moving the CJK-secret passes to run last in redact()'s pipeline.
        mac_glued = hook.redact("令牌hO9il6bkYaa:bb:cc:dd:ee:ff")
        self.assertNotIn("bb:cc:dd:ee:ff", mac_glued)
        self.assertNotIn("aa:bb:cc:dd:ee:ff", mac_glued)
        self.assertIn("[REDACTED_IP]", mac_glued)

    def test_cjk_secret_redaction_does_not_flag_english_words_after_keyword(self) -> None:
        # Round-2 P1: a bare "<keyword> <whitespace> <value>" mention with no separator token at
        # all previously matched *any* sufficiently-long ASCII run, including ordinary English/
        # technical words with no digit in them -- these must now stay completely unredacted.
        # Two independent guards close this: the connector's whitespace no longer crosses a
        # newline, and a value reached via bare whitespace (no explicit separator) must contain
        # an ASCII digit.
        unchanged_cases = (
            "## 密码\nbcrypt 是当前推荐的哈希算法",
            "密码\n\nsomething 完全无关的下一段",
            "密码 bcrypt 加盐存储更安全",
            "密钥 rotation 周期是 90 天",
            "令牌 refresh 机制需要重构",
            "密码 policy 要求至少 12 位",
            "| 密码策略 | bcrypt |",
            "密码：----",
            "密码：****",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_cjk_secret_redaction_handles_connector_word_plus_punctuation(self) -> None:
        # Round-2 P2: the round-1 connector accepted at most one token, so a connector word
        # immediately followed by a colon/equals ("是："/"为:"/"就是＝") matched nothing, and
        # common verb-phrase connectors ("设置为"/"改为"/"改成"/"更新为") weren't recognized.
        # The full-width equals sign "＝" (U+FF1D) alongside the already-handled full-width
        # colon is covered too.
        secret = "Tq4%wZ7nJ2bV"
        for text in (
            f"数据库密码是：{secret}",
            f"登录口令为：{secret}",
            f"数据库密码是:{secret}",
            f"密码就是：{secret}",
            f"密码设置为{secret}",
            f"密码改为{secret}",
            f"密码改成{secret}",
            f"密码更新为{secret}",
            f"密码＝{secret}",
        ):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)

    def test_cjk_secret_redaction_handles_quoted_fenced_and_symbol_led_values(self) -> None:
        # Round-2 P2: a value's leading character had to be alnum, so a backtick/quote-fenced
        # value or one that legitimately starts with a symbol (a strong-password policy's
        # leading "!") never matched; the minimum length (6) was also too high for a short
        # PIN/OTP.
        secret = "Tq4%wZ7nJ2bV"
        for text in (
            f"密码：`{secret}`",
            f'密码："{secret}"',
            f"密码：“{secret}”",
            f"| 密码 |`{secret}`|",
            f"| 密码 |**{secret}**|",
            f"密码：!{secret}",
        ):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)
        short_pin = hook.redact("密码：Ab3z")
        self.assertNotIn("Ab3z", short_pin)
        self.assertEqual(short_pin, "密码：[REDACTED]")

    def test_cjk_secret_redaction_handles_header_data_row_table_columns(self) -> None:
        # Round-2 P2: a real credentials table commonly puts the label in a header row and the
        # value in a separate data row, same column (e.g. an admin/staff pair) -- no single-row
        # regex can see across rows. Column tracking must find the value regardless of how many
        # other columns the table has, and regardless of which alignment-colon style the
        # separator row uses.
        secret = "Tq4%wZ7nJ2bV"
        three_col = hook.redact(
            f"| 账号 | 密码 | 备注 |\n|---|---|---|\n| admin | {secret} | 生产 |"
        )
        self.assertNotIn(secret, three_col)
        self.assertIn("[REDACTED]", three_col)
        self.assertIn("| admin |", three_col)
        self.assertIn("生产", three_col)
        aligned = hook.redact(f"| 账号 | 密码 |\n|:---|---:|\n| admin | {secret} |")
        self.assertNotIn(secret, aligned)
        self.assertIn("[REDACTED]", aligned)
        # A header row containing the keyword, with no data rows below it (or an unrelated
        # separator that never lines up), must not corrupt the header/separator rows themselves.
        header_only = "| 账号 | 密码 |\n|---|---|"
        self.assertEqual(hook.redact(header_only), header_only)

    def test_cjk_secret_redaction_handles_same_row_table_shape_gaps(self) -> None:
        # Round-2 P2: the old `trail` group required the value to be immediately followed by
        # "\s*|", so a trailing note in the cell, a row with no closing pipe (valid GFM), or a
        # second label/value pair sharing the first match's closing pipe all leaked. Dropping the
        # trailing-pipe requirement fixes all three at once.
        secret = "Tq4%wZ7nJ2bV"
        trailing_note = hook.redact(f"| 密码 |{secret} (已过期)|")
        self.assertNotIn(secret, trailing_note)
        self.assertIn("(已过期)", trailing_note)
        trailing_note_zh = hook.redact(f"| 密码 |{secret} 已重置|")
        self.assertNotIn(secret, trailing_note_zh)
        self.assertIn("已重置", trailing_note_zh)
        no_closing_pipe = hook.redact(f"| 密码 | {secret}")
        self.assertNotIn(secret, no_closing_pipe)
        self.assertEqual(no_closing_pipe, "| 密码 | [REDACTED]")
        second_secret = "9f3ac81de50b47c2a6e1d970bb42c8fe"
        two_pairs = hook.redact(f"| 密码 | {secret} | 密钥 | {second_secret} |")
        self.assertNotIn(secret, two_pairs)
        self.assertNotIn(second_secret, two_pairs)
        self.assertEqual(two_pairs, "| 密码 | [REDACTED] | 密钥 | [REDACTED] |")

    def test_cjk_secret_redaction_recognizes_wider_keyword_vocabulary(self) -> None:
        # Round-2 P2: the keyword vocabulary omitted 验证码 (the label the review's own
        # "OAuth-style device code" dogfood item most directly uses), several other common
        # password/key/code synonyms, and their Traditional-Chinese forms.
        secret = "Tq4%wZ7nJ2bV"
        keywords = (
            "验证码", "秘钥", "私钥", "凭证", "凭据", "激活码", "邀请码",
            "动态码", "设备码", "用户码", "密碼", "金鑰",
        )
        for keyword in keywords:
            text = f"{keyword}：{secret}"
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)

    def test_assignment_pattern_recognizes_fullwidth_separators(self) -> None:
        # Round-2 P2: `_ASSIGNMENT_RE`'s separator class was half-width ASCII only, so an ASCII
        # config label directly followed by a full-width colon/equals (routine output from a
        # Chinese IME) never matched, even though the equivalent half-width separator already
        # did.
        secret = "Tq4%wZ7nJ2bV"
        for text in (
            f"password：{secret}",
            f"passwd：{secret}",
            f"token：{secret}",
            f"api_key：{secret}",
            f"secret：{secret}",
            f"password ：{secret}",
            f"password＝{secret}",
        ):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)
        # The pre-existing half-width forms must still redact identically.
        self.assertEqual(
            hook.redact(f"password: {secret}"), f"password: [REDACTED]"
        )

    # --- dogfood dual-review round 3 (independent Claude opus + Codex, 2026-08-22, retry round):
    # findings against the round-2 patterns above. Every synthetic value below is newly constructed
    # for these tests, never a real secret either review round found. Each test in this section was
    # verified to FAIL against the pre-round-3 `claude_memory_hook.py` (temporarily reverted
    # locally) and PASS after the round-3 fix. ---

    def test_cjk_secret_value_class_covers_common_password_punctuation(self) -> None:
        # Round-3 P1 (items 1 & 10): the value body whitelist omitted 17 ASCII punctuation
        # characters real password generators routinely emit -- parens, brackets, braces, pipe,
        # semicolon, colon, comma, angle brackets, '?', backslash, quotes, backtick.
        # `redact('密码：Qz7(tW4mNe1R')` previously returned the input completely unchanged (a
        # full leak); a value containing '?' partway through leaked its own tail.
        paren = hook.redact("密码：Qz7(tW4mNe1R")
        self.assertNotIn("Qz7(tW4mNe1R", paren)
        self.assertEqual(paren, "密码：[REDACTED]")

        question_mark = hook.redact("密码：Qz7Tw?4mNe1R")
        self.assertNotIn("Qz7Tw?4mNe1R", question_mark)
        self.assertNotIn("4mNe1R", question_mark)
        self.assertEqual(question_mark, "密码：[REDACTED]")

        # A value using several of the newly-covered characters together.
        mixed = hook.redact("密码：Xk9?mQ2(vR8pL")
        self.assertNotIn("Xk9?mQ2(vR8pL", mixed)
        self.assertEqual(mixed, "密码：[REDACTED]")

        # Round-4 finding (items 2 & 8): this round closes the gap the comment above used to
        # describe. '[' / ']' now redact in full as ordinary value characters (guarded so they
        # can never swallow this file's own "[REDACTED...]" placeholders -- see the value-body
        # comment near `_CJK_VALUE_CHAR_CLASS_INLINE`/`_CJK_VALUE_CHAR_CLASS_TABLE`) instead of
        # truncating the value and leaking its tail next to a misleading "[REDACTED]" marker.
        bracket = hook.redact("密码：Qz7]tW4mNe1R")
        self.assertNotIn("Qz7]tW4mNe1R", bracket)
        self.assertNotIn("tW4mNe1R", bracket)
        self.assertEqual(bracket, "密码：[REDACTED]")

        bracket_open = hook.redact("密码：Tq49[zW7pNe2Vs")
        self.assertNotIn("Tq49[zW7pNe2Vs", bracket_open)
        self.assertNotIn("zW7pNe2Vs", bracket_open)
        self.assertEqual(bracket_open, "密码：[REDACTED]")

        # '|' is now an ordinary allowed value character in inline prose, where there is no cell
        # boundary to protect -- a pipe inside a real password is plausible and previously
        # truncated it the same way a bracket did.
        pipe_inline = hook.redact("密码：Tq49|zW7pNe2Vs")
        self.assertNotIn("Tq49|zW7pNe2Vs", pipe_inline)
        self.assertEqual(pipe_inline, "密码：[REDACTED]")
        # Inside a markdown table cell, '|' still stops a value at the cell boundary (unchanged --
        # an unescaped '|' there is GFM cell syntax, not part of the value): the leading run up to
        # it still redacts, the text after the pipe is a separate cell this pattern was never
        # meant to see.
        pipe_table = hook.redact("| 密码 | Tq49|zW7pNe2Vs |")
        self.assertNotIn("Tq49", pipe_table)
        self.assertIn("[REDACTED]", pipe_table)

    def test_cjk_secret_table_redacts_digitless_secrets(self) -> None:
        # Round-3 P1 (item 2 & 11): both the same-row and header/data-row table matchers required
        # an ASCII digit in the value (`_CJK_SECRET_VALUE_STRICT`), so a real digitless staff
        # password in a table leaked completely, even though the equivalent value behind an
        # explicit "：" separator in prose already redacted (permissive class).
        same_row = hook.redact("| 员工登录密码 | Adminadmin |")
        self.assertNotIn("Adminadmin", same_row)
        self.assertEqual(same_row, "| 员工登录密码 | [REDACTED] |")

        header_data = hook.redact(
            "| 账号 | 密码 |\n| --- | --- |\n| admin | letmeinplease |"
        )
        self.assertNotIn("letmeinplease", header_data)
        self.assertIn("[REDACTED]", header_data)

        # The compound-word false positive this fix must not reopen: switching the table value
        # class to permissive is only safe because the label-keyword match itself now requires a
        # *standalone* label (see `_CJK_SECRET_KEYWORD_STANDALONE`'s comment) -- "密码策略" no
        # longer counts as a label at all, so this pinned case must stay untouched.
        policy_row = "| 密码策略 | bcrypt |"
        self.assertEqual(hook.redact(policy_row), policy_row)

    def test_cjk_secret_table_column_scan_redacts_row_skipped_by_stray_separator(self) -> None:
        # Round-3 P2 (item 3): a genuine data row immediately followed by *another*
        # separator-shaped row (a malformed/pasted-transcript artifact) was misread as the start of
        # a new header and skipped via an `i += 2` step -- without ever being redacted, even though
        # `secret_cols` was already correctly established for it from the real header two rows up.
        text = "| 账号 | 密码 |\n| --- | --- |\n| admin | Qz7mQ2vR8 |\n| --- | --- |"
        redacted = hook.redact(text)
        self.assertNotIn("Qz7mQ2vR8", redacted)
        self.assertIn("| admin | [REDACTED] |", redacted)
        # Both separator rows survive untouched.
        self.assertEqual(redacted.count("| --- | --- |"), 2)

    def test_cjk_secret_table_column_scan_handles_missing_alignment_row(self) -> None:
        # Round-3 P2 (item 4): a pasted pseudo-table with no GFM alignment-dash row at all (very
        # common outside a Markdown editor) never armed the column scanner's `secret_cols` at all,
        # so its data row was invisible to it no matter what.
        text = "| 账号 | 密码 |\n| admin | Qz7mQ2vR8 |"
        redacted = hook.redact(text)
        self.assertNotIn("Qz7mQ2vR8", redacted)
        self.assertEqual(redacted, "| 账号 | 密码 |\n| admin | [REDACTED] |")

    def test_cjk_secret_table_column_scan_does_not_leak_into_trailing_prose(self) -> None:
        # Round-3 P3 (item 5(5)): a line was treated as a "table row" as soon as it contained a
        # single bare '|' anywhere, so ordinary prose with an incidental pipe right after a real
        # table was misread as a continuing data row and had a value redacted inside it.
        text = (
            "| 账号 | 密码 |\n| --- | --- |\n| admin | Ab1cdefg2 |\n"
            "运行 foo | bar1234 命令"
        )
        redacted = hook.redact(text)
        self.assertNotIn("Ab1cdefg2", redacted)
        self.assertIn("运行 foo | bar1234 命令", redacted)

    def test_cjk_secret_table_column_scan_does_not_flag_unrelated_compound_labels(self) -> None:
        # Round-3 P3 (items 5(3)/5(4)/13): the column scanner searched for the keyword as a bare
        # substring anywhere in the header cell, so a compound word merely containing the keyword
        # as a prefix ("令牌有效期" = "token validity period") was treated as a genuine secret
        # column, and a digit-bearing but non-secret value (a duration) in that column leaked.
        expiry = hook.redact(
            "| 项目 | 说明 |\n| --- | --- |\n| 密码策略 | bcrypt |\n| 令牌有效期 | 3600 |"
        )
        self.assertIn("3600", expiry)
        self.assertNotIn("[REDACTED]", expiry)

        course = hook.redact(
            "| 密码学课程 | 内容简介 |\n| --- | --- |\n| 网络安全 | 学习网络安全基础 |"
        )
        self.assertEqual(
            course,
            "| 密码学课程 | 内容简介 |\n| --- | --- |\n| 网络安全 | 学习网络安全基础 |",
        )

    def test_redacts_ascii_labeled_secret_in_table_cell(self) -> None:
        # Round-3 P1 (item 12): `_ASSIGNMENT_RE`'s ASCII keyword vocabulary
        # (password/passwd/pwd/secret/token/*_key) has the identical table-cell gap the CJK
        # vocabulary had before this round -- "| password | <value> |" leaked completely, since no
        # pattern in the CJK-secret block recognized an ASCII label, and `_ASSIGNMENT_RE` only
        # recognizes an explicit "="/":" separator, not a table cell.
        secret = "Xk9$mQ2vR8pL"
        redacted = hook.redact(f"| password | {secret} |")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "| password | [REDACTED] |")

        # A digitless ASCII-labeled secret must redact too (same fix as the CJK table case).
        digitless = hook.redact("| password | AlphaBetaGamma |")
        self.assertNotIn("AlphaBetaGamma", digitless)
        self.assertEqual(digitless, "| password | [REDACTED] |")

    def test_redacts_ascii_labeled_secret_in_inline_is_prose(self) -> None:
        # Round-3 P1 (item 12): the inline-prose sibling of the same gap -- "root password is
        # <value>" leaked completely.
        secret = "Xk9$mQ2vR8pL"
        redacted = hook.redact(f"root password is {secret}")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "root password is [REDACTED]")

    def test_ascii_inline_is_prose_does_not_flag_ordinary_english_sentences(self) -> None:
        # False-positive guard for the fix above: unlike the CJK inline pattern's "是"/"为", the
        # English word "is" is extremely common and ambiguous, so this new pattern deliberately
        # always requires an ASCII digit in the value (never the permissive digit-optional class)
        # regardless of whether "is" was present.
        unchanged_cases = (
            "the password is important",
            "the password is required",
            "the password is temporary",
            "my secret is out",
            "your api_key is invalid",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_cjk_secret_redaction_recognizes_round3_keyword_additions(self) -> None:
        # Round-3 P2 (item 6): the vocabulary omitted 助记词/恢复码/备份码/签名/PIN码.
        secret = "Tq4%wZ7nJ2bV"
        for keyword in ("助记词", "恢复码", "备份码", "签名", "PIN码", "pin码"):
            text = f"{keyword}：{secret}"
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)

    def test_cjk_secret_redaction_still_does_not_narrow_mac_or_ipv6_after_value_widening(
        self,
    ) -> None:
        # Round-3 regression guard: the value-class widening above must not reach into '[' / ']',
        # since every CJK-secret pass runs after `_MAC_ADDRESS_RE`/`_IPV6_CANDIDATE_RE` and would
        # otherwise swallow an already-inserted "[REDACTED_IP]" placeholder and downgrade it to a
        # generic "[REDACTED]". Re-run of the existing round-2 regression case as an explicit
        # round-3 checkpoint.
        mac_glued = hook.redact("令牌hO9il6bkYaa:bb:cc:dd:ee:ff")
        self.assertNotIn("bb:cc:dd:ee:ff", mac_glued)
        self.assertIn("[REDACTED_IP]", mac_glued)

    # --- dogfood dual-review round 4 (independent Claude opus + Codex, 2026-08-22, retry round):
    # findings against the round-3 patterns above. Every synthetic value below is newly constructed
    # for these tests, never a real secret either review round found. Each test in this section was
    # verified to FAIL against the pre-round-4 `claude_memory_hook.py` (temporarily reverted
    # locally via `git stash`) and PASS after the round-4 fix. ---

    def test_redact_is_idempotent_on_a_secret_shaped_markdown_table(self) -> None:
        # Round-4 blocking finding (items 1 & 12): `_redact_cjk_secret_table_columns`'s per-cell
        # substitution was unanchored and could re-match its own "[REDACTED]" output (the bare
        # word "REDACTED" is itself a run of allowed body characters), growing one bracket pair
        # per further `redact()` call -- `write_candidate_capture.py` calls `redact()` again on
        # samples that were already redacted at store time, so this was a real, live bug, not just
        # a theoretical one.
        text = "| 用户 | 密码 |\n|---|---|\n| root | Tq4zW7pNe2Vs |"
        once = hook.redact(text)
        twice = hook.redact(once)
        thrice = hook.redact(twice)
        self.assertEqual(once, "| 用户 | 密码 |\n|---|---|\n| root | [REDACTED] |")
        self.assertEqual(once, twice, "redact() must be idempotent on its own output")
        self.assertEqual(twice, thrice)
        self.assertNotIn("[[REDACTED]]", twice)

    def test_redact_does_not_downgrade_a_typed_placeholder_sharing_a_secret_column(self) -> None:
        # Round-4 blocking finding (item 12): the same bug also fired *within a single*
        # `redact()` call whenever a table cell tracked as a secret column already contained a
        # typed placeholder from an earlier pass in the pipeline (IPv4/IPv6/email/etc. all run
        # before the CJK-secret passes) -- the column scan matched the placeholder's own inner
        # word and downgraded "[REDACTED_IP]" to the generic "[[REDACTED]]".
        text = "| 密码 | 备注 |\n|---|---|\n| Tq4zW7pNe2Vs | fe80::1 |"
        redacted = hook.redact(text)
        self.assertNotIn("Tq4zW7pNe2Vs", redacted)
        self.assertNotIn("fe80::1", redacted)
        self.assertIn("[REDACTED_IP]", redacted, "typed placeholder must survive intact")
        self.assertNotIn("[[REDACTED", redacted)
        # And still idempotent on this shape.
        self.assertEqual(redacted, hook.redact(redacted))

    def test_cjk_secret_value_class_now_redacts_bracket_and_pipe_bearing_secrets_in_full(
        self,
    ) -> None:
        # Round-4 P1 finding (items 2 & 8): the value body's exclusion of '[' / ']' meant a real
        # secret containing one matched only up to that character, leaving the remainder in the
        # clear right next to a "[REDACTED]" marker that made the output *look* fully handled --
        # or, below the {4,} floor, leaked with no marker at all.
        for secret, text in (
            ("Tq49[zW7pNe2Vs", "密码：Tq49[zW7pNe2Vs"),
            ("Tq49]zW7pNe2Vs", "密码：Tq49]zW7pNe2Vs"),
            ("Tq4[zW7pNe2Vs", "密码：Tq4[zW7pNe2Vs"),  # was below the old {4,} floor
            ("Tq49|zW7pNe2Vs", "密码：Tq49|zW7pNe2Vs"),  # '|' now allowed inline (not table)
        ):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertEqual(redacted, "密码：[REDACTED]", text)

    def test_ascii_secret_vocabulary_now_recognizes_signature_and_bare_key(self) -> None:
        # Round-4 P1 finding (item 9): `_QUERY_SECRET_RE` already treated "signature" and bare
        # "key" as sensitive in a URL query string, but the shared ASCII keyword vocabulary used
        # by `_ASSIGNMENT_RE`/the table and inline-prose matchers omitted both, so the identical
        # label leaked completely in every other shape.
        secret = "Xk9mQ2vR8pL"
        for keyword in ("signature", "key"):
            colon = hook.redact(f"{keyword}：{secret}")
            self.assertNotIn(secret, colon, keyword)
            self.assertIn("[REDACTED]", colon, keyword)

            half_width = hook.redact(f"{keyword}: {secret}")
            self.assertNotIn(secret, half_width, keyword)
            self.assertEqual(half_width, f"{keyword}: [REDACTED]", keyword)

            table = hook.redact(f"| {keyword} | {secret} |")
            self.assertNotIn(secret, table, keyword)
            self.assertEqual(table, f"| {keyword} | [REDACTED] |", keyword)

            inline_is = hook.redact(f"the {keyword} is {secret}")
            self.assertNotIn(secret, inline_is, keyword)
            self.assertEqual(inline_is, f"the {keyword} is [REDACTED]", keyword)

    def test_bare_key_and_signature_do_not_flag_ordinary_english_sentences(self) -> None:
        # False-positive guard for the vocabulary widening above: "key" and "signature" are
        # ordinary English words, so a sentence using either with no secret-shaped value nearby --
        # and, for "key", no explicit separator/table-column context at all -- must stay unchanged.
        unchanged_cases = (
            "the key to success is hard work",
            "please sign the signature page below",
            "this is a key requirement for the release",
            "the digital signature verifies the document",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_cjk_secret_redaction_documents_known_residual_gaps_still_pass_through(self) -> None:
        # Round-4 items 3 & 10/4: explicitly-accepted, documented residual gaps (not blocking, not
        # regressions) re-verified against the round-4 code so a future round can see their exact
        # current behavior rather than relying on the comments alone.
        # Item 3: a label alone on one line with its value on the next is not covered (the
        # connector never crosses a newline -- a deliberate round-2 false-positive guard).
        self.assertEqual(hook.redact("密码\nTq4zW7pNe2Vs"), "密码\nTq4zW7pNe2Vs")
        # Items 4 & 10: `secret_cols` can persist from one table into an immediately adjacent,
        # unrelated table when no blank/non-table line separates them and the second table's own
        # header has no keyword cell -- over-redaction (the safe direction), not a leak.
        bleed = hook.redact(
            "| 用户 | 密码 |\n| root | Sec9retVal |\n| 项目 | 版本 |\n| orca | v1.2.3 |"
        )
        self.assertEqual(
            bleed,
            "| 用户 | 密码 |\n| root | [REDACTED] |\n| 项目 | 版本 |\n| orca | [REDACTED] |",
        )

    # --- dogfood dual-review round 5 (independent Claude opus + Codex, 2026-08-22, retry round):
    # findings against the round-4 patterns above. Every synthetic value below is newly constructed
    # for these tests, never a real secret either review round found. Each test in this section was
    # verified to FAIL against the pre-round-5 `claude_memory_hook.py` (temporarily reverted
    # locally) and PASS after the round-5 fix. ---

    def test_cjk_secret_table_column_scan_handles_row_with_no_trailing_pipe(self) -> None:
        # Round-5 P1 (items 1 & 2): `_split_table_row_cells` required a stripped line to both
        # start *and* end with '|', so a GFM data row typed/pasted without the trailing pipe was
        # not recognized as a table row at all -- its own value never redacted.
        secret = "Qw7#zP2mLv8Ke"
        no_trailing_pipe = hook.redact(f"| 用户 | 密码 |\n|---|---|\n| root | {secret}")
        self.assertNotIn(secret, no_trailing_pipe)
        self.assertEqual(no_trailing_pipe, "| 用户 | 密码 |\n|---|---|\n| root | [REDACTED]")

    def test_cjk_secret_table_column_scan_survives_a_single_cell_note_row(self) -> None:
        # Round-5 P1 (item 2, consequence a): `_split_table_row_cells` also required 2+ cells, so
        # a bare single-cell note row sandwiched between two real data rows of the same table
        # ("| 以下为备用账号 |") was itself misread as "not a table", resetting `secret_cols` and
        # letting the *next* row's password leak completely.
        first_secret = "Qw7#zP2mLv8Ke"
        second_secret = "Bd6@kR3wYt"
        text = (
            "| 用户 | 密码 |\n|---|---|\n"
            f"| root | {first_secret} |\n"
            "| 以下为备用账号 |\n"
            f"| backup | {second_secret} |"
        )
        redacted = hook.redact(text)
        self.assertNotIn(first_secret, redacted)
        self.assertNotIn(second_secret, redacted)
        self.assertEqual(
            redacted,
            "| 用户 | 密码 |\n|---|---|\n"
            "| root | [REDACTED] |\n"
            "| 以下为备用账号 |\n"
            "| backup | [REDACTED] |",
        )

    def test_cjk_secret_table_column_scan_handles_single_column_table(self) -> None:
        # Round-5 P1 (item 2, consequence b): a single-column credentials table -- header, dash
        # row, and data row all exactly one cell wide -- never had 2+ cells on any row, so it was
        # invisible to the column scanner no matter what, and no other pattern could see it either
        # (the same-row pattern's value class stops at the next line's leading '|').
        secret = "Qw7#zP2mLv8Ke"
        redacted = hook.redact(f"| 密码 |\n|---|\n| {secret} |")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "| 密码 |\n|---|\n| [REDACTED] |")

    def test_cjk_inline_secret_redaction_handles_bracketed_qualifier_before_separator(self) -> None:
        # Round-5 P2 (item 3): a label carrying a parenthetical/bracketed qualifier before its real
        # separator (an environment/scope note) had no way for the connector to skip past it, so
        # the value-matching attempt started on the qualifier's own opening bracket and failed
        # entirely, leaking the value completely.
        secret = "Qw7#zP2mLv8Ke"
        for text in (
            f"密码（生产）：{secret}",
            f"密码(prod)：{secret}",
            f"密码【prod】：{secret}",
            f"密码 (生产环境)：{secret}",
        ):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)
        # The qualifier itself, including its brackets, must survive untouched.
        self.assertEqual(hook.redact(f"密码（生产）：{secret}"), "密码（生产）：[REDACTED]")

    def test_cjk_inline_secret_redaction_recognizes_ji_connector(self) -> None:
        # Round-5 P2 (item 3): "即" ("namely"/"which is"), often preceded by a Chinese pause comma,
        # is a common connector this vocabulary omitted.
        secret = "Qw7#zP2mLv8Ke"
        redacted = hook.redact(f"服务器密码，即{secret}")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "服务器密码，即[REDACTED]")

    def test_assignment_value_class_now_redacts_bracket_and_brace_bearing_secrets(self) -> None:
        # Round-5 P1/P2 (item 11): `_ASSIGNMENT_RE`'s own value class excluded '['/']'/'{'/'}' as
        # part of its structural-JSON exclusion set, so a real secret merely *containing* one of
        # those characters matched only up to it and leaked the remainder in the clear next to a
        # "[REDACTED]" marker that made the output look fully handled.
        for secret, text in (
            ("Aa7[BB8N", "password=Aa7[BB8N"),
            ("Aa7]BB8N", "password=Aa7]BB8N"),
            ("Aa7{BB8N", "password=Aa7{BB8N"),
            ("Aa7}BB8N", "password=Aa7}BB8N"),
        ):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertEqual(redacted, "password=[REDACTED]", text)
        # The query-string '&key=value' boundary this exclusion set also protects must still hold.
        curl = hook.redact("token=abc123&next=xyz&other=1")
        self.assertEqual(curl, "token=[REDACTED]&next=xyz&other=1")

    def test_cjk_secret_value_class_now_redacts_known_ignorecase_homoglyphs(self) -> None:
        # Round-5 P2 (item 8): the CJK-secret value classes are a positive ASCII-only whitelist, so
        # a secret containing one of the four IGNORECASE-tainted homoglyphs this file's own
        # boundary lookarounds already guard against elsewhere (U+0130/U+0131/U+017F/U+212A)
        # matched only up to that character and fell below the {4,} floor -- leaking the value
        # completely, even though the byte-identical secret under `_ASSIGNMENT_RE`'s ASCII path
        # already redacted it in full.
        homoglyphs = {
            "İ": "LATIN CAPITAL LETTER I WITH DOT ABOVE",
            "ı": "LATIN SMALL LETTER DOTLESS I",
            "ſ": "LATIN SMALL LETTER LONG S",
            "K": "KELVIN SIGN",
        }
        unicodedata = __import__("unicodedata")
        for char, expected_name in homoglyphs.items():
            self.assertEqual(unicodedata.name(char), expected_name)
            secret = f"Qw7{char}zP2mLv8Ke"
            colon = hook.redact(f"密码：{secret}")
            self.assertNotIn(secret, colon, expected_name)
            self.assertEqual(colon, "密码：[REDACTED]", expected_name)
            table = hook.redact(f"| 密码 | {secret} |")
            self.assertNotIn(secret, table, expected_name)
            self.assertEqual(table, "| 密码 | [REDACTED] |", expected_name)
            inline = hook.redact(f"root密码是{secret}")
            self.assertNotIn(secret, inline, expected_name)
            self.assertEqual(inline, "root密码是[REDACTED]", expected_name)

    def test_table_cjk_secret_value_class_handles_escaped_pipe_in_cell(self) -> None:
        # Round-5 P2 (item 9): a real markdown table cell can legitimately contain a
        # backslash-escaped pipe (CommonMark's way of putting a literal '|' in a cell), but the
        # table value class read the escaped pipe's own '|' as an unconditional hard stop.
        secret = "Qx9\\|Lm2N7"
        redacted = hook.redact(f"| 密码 | {secret} |")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "| 密码 | [REDACTED] |")

    def test_secret_label_keyword_standalone_rejects_ascii_compound_continuation(self) -> None:
        # Round-5 P2 (item 12): the ASCII/CJK "standalone" guards only rejected a keyword
        # immediately followed by another alnum character (ASCII) or another CJK ideograph (CJK),
        # so a compound identifier neither vocabulary has a dedicated alternative for --
        # "password_policy", "api_key_format", a CJK label glued to an ASCII qualifier
        # ("密码policy") -- still counted as a genuine standalone secret label and armed an entire
        # markdown documentation-table column that was never a secret column at all.
        unchanged_cases = (
            "| password_policy | min-12-chars |",
            "| api_key_format | uuid-v4 |",
            "| 密码policy | bcrypt |",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)
        # A bare keyword (no compounding) must still redact exactly as before.
        secret = "Xk9mQ2vR8pL"
        still_redacts = hook.redact(f"| password | {secret} |")
        self.assertNotIn(secret, still_redacts)
        self.assertEqual(still_redacts, "| password | [REDACTED] |")

    def test_cjk_secret_redaction_documents_further_known_residual_gaps(self) -> None:
        # Round-5 items re-verified as explicitly-accepted, documented residual gaps (not
        # blocking, not regressions) rather than fixed this round, so a future round can see their
        # exact current behavior rather than relying on the comments alone.
        # Item 4: a bare ASCII "key" (no compounding) column header still arms the whole column --
        # over-redaction (the safe direction, consistent with this file's stated bias), not a leak.
        key_column = hook.redact("| Key | Description |\n|---|---|\n| retries | how many |")
        self.assertEqual(
            key_column,
            "| Key | [REDACTED] |\n|---|---|\n| [REDACTED] | how many |",
        )
        # Item 12(a): an explicit CJK separator followed by a single plausible-looking (but
        # non-secret) English word still redacts that word -- the permissive value class an
        # explicit separator selects has no way to distinguish a real secret from an ordinary
        # technical term with no digit in it.
        self.assertEqual(
            hook.redact("当前密码：bcrypt 哈希算法需要升级"),
            "当前密码：[REDACTED] 哈希算法需要升级",
        )
        # Item 10: a real secret that happens to literally contain this file's own placeholder
        # shape ("[REDACTED_TOKEN]") as a substring defeats the tempered-greedy-token guard that
        # exists to keep `redact()` idempotent -- an adversarial corner case, not a real-world
        # secret shape.
        self.assertEqual(
            hook.redact("密码：Aa1[REDACTED_TOKEN]Zz9"),
            "密码：Aa1[REDACTED_TOKEN]Zz9",
        )

    def test_split_blocks_does_not_treat_non_newline_separators_as_heading_boundaries(
        self,
    ) -> None:
        # P1: split_blocks() used str.splitlines(), which breaks on more
        # characters (U+2028, U+2029, \v, \f, ...) than real Markdown or
        # this project's own MEMORY.md convention treats as a line break. A
        # body paragraph containing one of those characters, immediately
        # followed by text starting with '#', could get read as a genuine
        # heading that the note's author never wrote -- spoofing the
        # "section" label shown to Codex and gaining rank_blocks()'s 3x
        # heading-match scoring bonus on content that never earned it.
        # U+2028 (LINE SEPARATOR) is exactly such a character: real
        # Markdown treats it as ordinary text, but str.splitlines() (unlike
        # a plain '\n' split) breaks on it too.
        text = "# real heading\nbody line one # spoofed heading\nbody line two\n"
        doc = hook.MemoryDocument("ref", text, 0)
        blocks = list(hook.split_blocks(doc))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].heading, "real heading")
        self.assertIn("spoofed heading", blocks[0].text)

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

    def test_ipv4_redaction_not_defeated_by_adjacent_non_ascii_digit(self) -> None:
        # Exhaustive-audit finding (independent dual review, 2026-08-20, P2-2): every `\d` in the
        # old `_IPV4_RE` -- the boundary lookarounds *and* the three octet alternatives themselves
        # -- was Python's default Unicode-aware `\d`, which matches decimal digits from many
        # non-ASCII scripts, not just ASCII 0-9. A real IPv4 address directly preceded by one of
        # those non-ASCII digits (no separating space) made the leading lookbehind treat the
        # address as a continuation of a longer digit run, so the whole address leaked completely
        # unredacted. Reproduced across 8 unrelated digit scripts below; fixed by replacing every
        # `\d` in the pattern (lookarounds and octet bodies alike) with an explicit `[0-9]` class,
        # which never matches a non-ASCII digit regardless of flags.
        unicodedata = __import__("unicodedata")
        non_ascii_fives = {
            "Arabic-Indic": "٥",
            "Extended Arabic-Indic (Persian)": "۵",
            "Devanagari": "५",
            "Bengali": "৫",
            "Ol Chiki": "᱕",
            "Fullwidth": "５",
            "Thai": "๕",
            "Mathematical Bold": "\U0001d7d3",
        }
        for label, digit in non_ascii_fives.items():
            self.assertEqual(unicodedata.category(digit), "Nd", label)
            redacted = hook.redact(f"leaked at {digit}192.168.0.1 today")
            self.assertNotIn("192.168.0.1", redacted, label)
            self.assertIn("[REDACTED_IP]", redacted, label)
            # The non-ASCII digit itself is untouched -- only the real address is redacted.
            self.assertIn(digit, redacted, label)

        # Sanity: an ASCII digit glued directly onto the address (no separator) must still NOT
        # be redacted -- deliberate, pre-existing behavior (the address reads as part of a longer
        # digit run), unchanged by this fix.
        unchanged = "leaked at 5192.168.0.1 today"
        self.assertEqual(hook.redact(unchanged), unchanged)

    def test_ipv4_redaction_does_not_swallow_fullwidth_digit_prose(self) -> None:
        # Exhaustive-audit finding (independent dual review, 2026-08-20, P2-2), reverse direction
        # of the false negative above: since the old `_IPV4_RE`'s octet alternatives also matched
        # non-ASCII digits via bare `\d`, a fullwidth-digit "version number" written in CJK prose
        # with ordinary ASCII dots (each octet needing only 1-2 digits, which the pattern's
        # `1?\d?\d` branch can satisfy without ever needing a literal ASCII "1"/"2" prefix) was
        # incorrectly matched and redacted as though it were a real IP address, corrupting
        # unrelated text. Lower priority than the false negative above (noted as such by the
        # review) but fixed by the same `[0-9]`-only change, so covered here too.
        two_digit_octets = "５６.６８.１２.３４"  # "56.68.12.34"
        text = f"当前版本号是{two_digit_octets},请注意"
        redacted = hook.redact(text)
        self.assertEqual(redacted, text)
        self.assertNotIn("[REDACTED_IP]", redacted)

        one_digit_octets = "５.６.０.１"  # "5.6.0.1"
        text2 = f"版本{one_digit_octets}发布了"
        redacted2 = hook.redact(text2)
        self.assertEqual(redacted2, text2)
        self.assertNotIn("[REDACTED_IP]", redacted2)

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
        sid = "22222222-2222-2222-2222-222222222222"
        tail = (
            json.dumps({"type": "other", "relocatedCwd": "/tmp/attackws", "sessionId": sid})
            + "\n"
            + json.dumps({"type": "relocated", "relocatedCwd": "/tmp/legitws", "sessionId": sid})
        ).encode("utf-8")
        self.assertEqual(hook._find_relocated_cwd(tail, sid), "/tmp/legitws")

    def test_transcript_scan_prefers_newest_and_scans_more_than_eight(self) -> None:
        # N6: scanning was capped at 8 transcripts sorted by (arbitrary
        # UUID) filename, so a legitimate owner's own transcript could sort
        # after the cap purely by chance and be refused. 20 decoys (older
        # sessions of the *same* real workspace -- recording the same cwd,
        # not a different one, since a genuinely different recorded cwd in
        # the same directory now means something else entirely: see
        # test_two_genuinely_colliding_cwds_are_both_refused below) with
        # old mtimes; the real owner's transcript is the most recently
        # written.
        cwd = "/tmp/many-transcripts-owner"
        dirname = hook.claude_project_dirname(cwd)
        project_dir = self.fixture.source / dirname
        project_dir.mkdir(mode=0o700, parents=True)
        old_time = 1_700_000_000.0
        for index in range(20):
            decoy_id = f"33333333-3333-3333-3333-{index:012d}"
            decoy = project_dir / f"{decoy_id}.jsonl"
            decoy.write_text(json.dumps({"type": "attachment", "cwd": cwd, "sessionId": decoy_id}) + "\n")
            decoy.chmod(0o600)
            os.utime(decoy, (old_time + index, old_time + index))
        self.fixture.add_session_transcript(
            cwd=cwd, dirname=dirname, session_id="44444444-4444-4444-4444-444444444444"
        )
        self.assertTrue(hook._session_recorded_cwd_matches(project_dir, cwd))

    def test_two_genuinely_colliding_cwds_are_both_refused(self) -> None:
        # P1-R2-1 (independent Codex sol/xhigh review, 2026-08-17, round 2):
        # _session_recorded_cwd_matches() alone correctly failed closed when
        # a colliding cwd had never actually run a session in the shared
        # directory -- but if *both* colliding cwds genuinely ran real
        # sessions there, each with its own honest transcript, a request
        # from either one still matched and still received the one shared
        # MEMORY.md, which may hold the other cwd's notes. Two distinct,
        # real cwd values that sanitize to the same directory, each with
        # its own real transcript recording its own real cwd: now *neither*
        # can retrieve memory through this bridge, not just the one whose
        # transcript is missing.
        cwd_a = "/tmp/collision/team/app"
        cwd_b = "/tmp/collision/team-app"
        self.assertEqual(hook.claude_project_dirname(cwd_a), hook.claude_project_dirname(cwd_b))
        self.fixture.add_memory("# shared\nvictim or attacker, either way this must not leak", cwd=cwd_a)
        self.fixture.add_session_transcript(
            cwd=cwd_b,
            dirname=hook.claude_project_dirname(cwd_b),
            session_id="77777777-7777-7777-7777-777777777777",
        )
        self.assertEqual(self.fixture.run("shared", cwd=cwd_a), "")
        self.assertEqual(self.fixture.run("shared", cwd=cwd_b), "")

    def test_conflict_beyond_the_old_scan_cap_is_still_detected(self) -> None:
        # R3-P1-1 (independent Claude opus5/max review, 2026-08-17, round
        # 3): the round-2 fix above stopped scanning after
        # MAX_TRANSCRIPTS_SCANNED_PER_PROJECT (then 16) transcripts, so a
        # shared directory whose *only* conflicting transcript sorted past
        # the cap was never seen, and the collision-refusal silently didn't
        # fire -- reproduced on a real, already-existing collision on this
        # machine that was ~6 ordinary sessions away from crossing that
        # cap. 20 owner transcripts (more than the old cap, comfortably
        # inside the new one) plus exactly one transcript recording a
        # genuinely different real cwd, given the *oldest* mtime so it
        # would have sorted dead last under the old newest-first cap.
        cwd = "/tmp/many-transcripts-collision/team/app"
        other_cwd = "/tmp/many-transcripts-collision/team-app"
        self.assertEqual(hook.claude_project_dirname(cwd), hook.claude_project_dirname(other_cwd))
        dirname = hook.claude_project_dirname(cwd)
        project_dir = self.fixture.source / dirname
        project_dir.mkdir(mode=0o700, parents=True)
        old_time = 1_700_000_000.0
        conflicting = project_dir / "22222222-2222-2222-2222-222222222222.jsonl"
        conflicting.write_text(
            json.dumps(
                {"type": "attachment", "cwd": other_cwd, "sessionId": "22222222-2222-2222-2222-222222222222"}
            )
            + "\n"
        )
        conflicting.chmod(0o600)
        os.utime(conflicting, (old_time, old_time))
        for index in range(20):
            decoy_id = f"33333333-3333-3333-3333-{index:012d}"
            decoy = project_dir / f"{decoy_id}.jsonl"
            decoy.write_text(json.dumps({"type": "attachment", "cwd": cwd, "sessionId": decoy_id}) + "\n")
            decoy.chmod(0o600)
            os.utime(decoy, (old_time + 1_000 + index, old_time + 1_000 + index))
        self.assertFalse(hook._session_recorded_cwd_matches(project_dir, cwd))
        self.assertFalse(hook._session_recorded_cwd_matches(project_dir, other_cwd))

    def test_scan_fails_closed_when_transcript_count_exceeds_the_cap(self) -> None:
        # R3-P1-1's other half: even when every transcript in a directory
        # agrees (no genuine conflict), a directory holding more transcripts
        # than MAX_TRANSCRIPTS_SCANNED_PER_PROJECT cannot be proven
        # conflict-free within budget and must fail closed outright, not
        # fall back to scanning a subset and trusting it.
        cwd = "/tmp/too-many-transcripts-owner"
        dirname = hook.claude_project_dirname(cwd)
        project_dir = self.fixture.source / dirname
        project_dir.mkdir(mode=0o700, parents=True)
        total = hook.MAX_TRANSCRIPTS_SCANNED_PER_PROJECT + 1
        for index in range(total):
            session_id = f"33333333-3333-4333-8333-{index:012d}"
            transcript = project_dir / f"{session_id}.jsonl"
            transcript.write_text(json.dumps({"type": "attachment", "cwd": cwd, "sessionId": session_id}) + "\n")
            transcript.chmod(0o600)
        self.assertFalse(hook._session_recorded_cwd_matches(project_dir, cwd))

    def test_relocated_marker_does_not_manufacture_a_false_collision_for_the_owner(self) -> None:
        # R3-P2-1 (independent Claude opus5/max review, 2026-08-17, round
        # 3): the round-3 collision-refusal fix above used the
        # relocated-priority recorded cwd for *both* matching and conflict
        # detection. A transcript that plainly recorded the owner's own cwd
        # but *also* carried an unrelated "relocated" marker (its own later
        # history, not a second workspace) then looked like a second,
        # distinct occupant of the directory and got the genuine owner
        # refused too. Session 1 plainly records the owner's cwd with no
        # relocation; session 2 also plainly records the owner's cwd but
        # additionally relocated (mid-session) to a wholly unrelated cwd.
        cwd = "/tmp/owner-with-a-relocated-session"
        unrelated_cwd = "/tmp/somewhere-else-entirely"
        self.fixture.add_memory("# topic\nowner's own note", cwd=cwd, with_transcript=False)
        self.fixture.add_session_transcript(
            cwd=cwd,
            dirname=hook.claude_project_dirname(cwd),
            session_id="11111111-1111-1111-1111-111111111111",
        )
        self.fixture.add_session_transcript(
            cwd=cwd,
            dirname=hook.claude_project_dirname(cwd),
            session_id="66666666-6666-6666-6666-666666666666",
            relocated_cwd=unrelated_cwd,
        )
        output = self.fixture.run("topic", cwd=cwd)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("owner's own note", context)

    def test_forged_transcript_with_no_matching_session_id_is_not_trusted(self) -> None:
        # A same-OS-user adversary with write access to a Claude project
        # directory (any code executing as the invoking user already has
        # this -- no elevated privilege needed) used to be able to defeat
        # the whole collision defense with one forged line:
        # `echo '{"cwd":"<target>"}' > forged.jsonl`. Requiring the record's
        # own sessionId to match the file's name (see _SESSION_ID_FORMAT_RE)
        # closes the naive form of that forgery: a file with no sessionId
        # field at all, or a non-UUID filename, is never trusted regardless
        # of what "cwd" it claims (independent finding, 2026-08-17, via a
        # dedicated full-audit Workflow, confirmed_real after adversarial
        # re-verification).
        cwd = "/tmp/forgery-target"
        dirname = hook.claude_project_dirname(cwd)
        project_dir = self.fixture.source / dirname
        project_dir.mkdir(mode=0o700, parents=True)
        forged = project_dir / "forged.jsonl"
        forged.write_text(json.dumps({"type": "attachment", "cwd": cwd}) + "\n")
        forged.chmod(0o600)
        self.assertFalse(hook._session_recorded_cwd_matches(project_dir, cwd))
        # Even a UUID-shaped filename doesn't help without the matching
        # internal sessionId field.
        forged_uuid = project_dir / "55555555-5555-5555-5555-555555555555.jsonl"
        forged_uuid.write_text(json.dumps({"type": "attachment", "cwd": cwd}) + "\n")
        forged_uuid.chmod(0o600)
        self.assertFalse(hook._session_recorded_cwd_matches(project_dir, cwd))

    def test_invalid_utf8_in_a_transcript_line_never_synthesizes_a_different_cwd(self) -> None:
        # A transcript line decoded with an "ignore" fallback on invalid
        # UTF-8 could silently drop just the bad byte(s), turning a
        # malformed value into a different, coincidentally valid string --
        # e.g. an invalid byte inside "team-<0xFF>app" collapsing to the
        # real string "team-app", which might legitimately be some other
        # workspace's cwd. That would let a corrupted (or deliberately
        # malformed) transcript line masquerade as an honest record of a
        # cwd it never actually recorded (independent Codex sol/xhigh
        # review, 2026-08-17, round 2, T3). Decoding is per-line and
        # strict: a line with invalid bytes is discarded outright, never
        # repaired into something that might match.
        target_cwd = "/tmp/invalid/team-app"
        dirname = hook.claude_project_dirname(target_cwd)
        project_dir = self.fixture.source / dirname
        project_dir.mkdir(mode=0o700, parents=True)
        sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        transcript = project_dir / f"{sid}.jsonl"
        prefix = (
            b'{"type":"attachment","sessionId":"' + sid.encode() + b'","cwd":"/tmp/invalid/team-'
        )
        malformed = prefix + bytes([0xFF]) + b'app"}'
        transcript.write_bytes(malformed + b"\n")
        transcript.chmod(0o600)
        self.assertFalse(hook._session_recorded_cwd_matches(project_dir, target_cwd))

    def test_jsonl_line_scan_matches_js_newline_only_splitting(self) -> None:
        # N7: Python's str.splitlines() breaks on more characters (U+2028,
        # U+2029, \v, \f, ...) than JS's plain '\n' scanning does. A record
        # whose string value happens to contain one of those must still be
        # treated as a single JSONL line.
        sid = "66666666-6666-6666-6666-666666666666"
        text = json.dumps({"type": "attachment", "cwd": "/tmp/x y", "sessionId": sid})
        self.assertEqual(len(hook._jsonl_lines(text.encode("utf-8"))), 1)
        self.assertEqual(
            hook._find_json_field(text.encode("utf-8"), hook._CWD_FIELD_RE, "cwd", forward=True, expected_session_id=sid),
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


class DiskVolumeUuidPerCallerTimeoutTests(unittest.TestCase):
    """Round fix (2026-08-20): `_disk_volume_uuid`'s internal `diskutil` timeout is now
    per-caller instead of one shared constant. Independent review found the previous shared
    15s bound was correct for the SessionEnd/write-trigger path (genuinely unbounded latency,
    no outer cap) but a regression for the ALREADY-LIVE UserPromptSubmit path, which the real,
    installed `~/.codex/hooks.json` wraps in its own outer `"timeout": 5` at the Codex-hook
    level (install_bridge.py's `DEFAULT_HOOK_TIMEOUT`): a call landing in the 5-15s range used
    to exit cleanly on its own terms at the old 2s internal bound, but with a shared 15s
    internal bound instead runs past the outer cap and gets hard-killed by Codex's own
    enforcement.

    These tests mock `subprocess.run` to control the simulated diskutil response and assert
    which literal `timeout` value each real call path's own default `volume_uuid_reader`
    actually passes through -- never waiting real seconds, and never touching a real `diskutil`.
    """

    @staticmethod
    def _fake_completed_process() -> mock.Mock:
        result = mock.Mock()
        result.stdout = plistlib.dumps({"VolumeUUID": TEST_UUID})
        return result

    def _fake_run_capturing_timeout(self, captured: dict[str, object]):
        def fake_run(_cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return self._fake_completed_process()

        return fake_run

    def test_module_constants_leave_real_headroom_under_the_live_outer_5s_hook_cap(self) -> None:
        # The outer cap is install_bridge.py's own DEFAULT_HOOK_TIMEOUT, applied to the real,
        # live UserPromptSubmit handler (make_handler()'s own default) -- confirmed directly
        # against that constant, not assumed to be 5.
        import install_bridge as installer

        self.assertEqual(installer.DEFAULT_HOOK_TIMEOUT, 5)
        self.assertEqual(hook._DISKUTIL_TIMEOUT_USER_PROMPT_SUBMIT, 4)
        self.assertEqual(hook._DISKUTIL_TIMEOUT_SESSION_END, 15)
        self.assertLess(hook._DISKUTIL_TIMEOUT_USER_PROMPT_SUBMIT, installer.DEFAULT_HOOK_TIMEOUT)
        self.assertGreater(hook._DISKUTIL_TIMEOUT_SESSION_END, hook._DISKUTIL_TIMEOUT_USER_PROMPT_SUBMIT)

    def test_user_prompt_submit_default_reader_passes_the_tight_timeout(self) -> None:
        captured: dict[str, object] = {}
        with mock.patch.object(hook.subprocess, "run", side_effect=self._fake_run_capturing_timeout(captured)):
            result = hook._disk_volume_uuid_user_prompt_submit(Path("/Volumes/Extreme SSD"))
        self.assertEqual(result, TEST_UUID)
        self.assertEqual(captured["timeout"], hook._DISKUTIL_TIMEOUT_USER_PROMPT_SUBMIT)

    def test_session_end_default_reader_passes_the_loose_timeout(self) -> None:
        captured: dict[str, object] = {}
        with mock.patch.object(hook.subprocess, "run", side_effect=self._fake_run_capturing_timeout(captured)):
            result = hook._disk_volume_uuid_session_end(Path("/Volumes/Extreme SSD"))
        self.assertEqual(result, TEST_UUID)
        self.assertEqual(captured["timeout"], hook._DISKUTIL_TIMEOUT_SESSION_END)

    def test_verify_storage_own_default_is_the_user_prompt_submit_reader(self) -> None:
        default = inspect.signature(hook.verify_storage).parameters["volume_uuid_reader"].default
        self.assertIs(default, hook._disk_volume_uuid_user_prompt_submit)

    def test_run_own_default_is_the_user_prompt_submit_reader(self) -> None:
        # This is the literal default main() uses for the real, live UserPromptSubmit
        # invocation -- main() never overrides volume_uuid_reader itself.
        default = inspect.signature(hook.run).parameters["volume_uuid_reader"].default
        self.assertIs(default, hook._disk_volume_uuid_user_prompt_submit)

    def test_run_without_override_reaches_the_tight_timeout_and_still_works_at_normal_speed(self) -> None:
        # End-to-end through run()'s own real default (no volume_uuid_reader override, exactly
        # how main() calls it), with subprocess.run mocked to a normal-speed valid response --
        # confirms the tighter bound doesn't regress the ordinary, ok case.
        fixture = HookFixture()
        self.addCleanup(fixture.close)
        fixture.add_memory("# topic\ndetail")
        policy, policy_sha = fixture.policy()
        raw = json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "topic", "cwd": fixture.cwd}
        ).encode()

        captured: dict[str, object] = {}
        with mock.patch.object(hook.subprocess, "run", side_effect=self._fake_run_capturing_timeout(captured)):
            output = hook.run(
                policy_path=policy,
                expected_policy_sha256=policy_sha,
                expected_script_sha256=fixture.script_sha,
                stdin=raw,
                script_path=fixture.script,
            )
        self.assertEqual(captured["timeout"], hook._DISKUTIL_TIMEOUT_USER_PROMPT_SUBMIT)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("detail", context)


if __name__ == "__main__":
    unittest.main()
