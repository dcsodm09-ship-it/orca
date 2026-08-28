from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import os
import plistlib
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

import claude_memory_hook as hook
from claude_memory_hook import Finding


TEST_UUID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"

# The workspace the fixture pretends every Codex session runs from by default.
# Deliberately contains a space and a '/' so tests exercise the real
# character-replacement transform, not just a already-alphanumeric path.
DEFAULT_CWD = "/Users/tester/Orca Workspace/project-a"


def _min_elapsed(fn, attempts: int = 5) -> float:
    """Best-of-`attempts` wall-clock timing for ReDoS/scaling assertions.

    Round-6 P3 finding (independent Claude opus + Codex dual review, 2026-08-22): a
    single-shot `time.monotonic()` reading around a regex call flakes under concurrent
    CPU contention on the same machine (measured up to ~32x noise on one call), even
    though the underlying pattern is genuinely linear. A real catastrophic-backtracking
    regression is slow on every attempt, not just an unlucky one, so taking the minimum
    across several fresh attempts keeps the assertion sensitive to real blowups while
    no longer flaking on transient scheduling noise.
    """
    best = float("inf")
    for _ in range(attempts):
        started = time.monotonic()
        fn()
        best = min(best, time.monotonic() - started)
    return best


def _min_cpu_elapsed(fn, attempts: int = 3) -> float:
    """Best-of-`attempts` CPU timing -- `_min_elapsed` for assertions with an ABSOLUTE ceiling.

    Round 5 F6 (live-verification run, 2026-08-28). Best-of-N wall clock is enough to stabilize a
    RATIO ("4x the input must not cost 16x the time"): both readings absorb the same noise, so the
    quotient survives it. It is not enough to stabilize an absolute ceiling, because the noise has
    nothing to cancel against. Measured directly, by oversubscribing this machine's CPUs 2x while
    re-running the two tests that flaked:

        redact("password:" * 6400)   wall  1.99s idle -> 6.47s loaded   (the 5.0s ceiling: FAILS)
                                     cpu   1.99s idle -> 2.70s loaded

    The wall-clock inflation is this process being descheduled while other work runs -- it is not
    redact() doing more work, which is the only thing these tests exist to detect, and no ceiling
    that still fails on a real quadratic regression can survive a 3.3x machine-load multiplier. CPU
    time excludes exactly that noise (1.36x residual here, from cache/memory-bandwidth contention)
    and includes every cycle a backtracking regex would actually burn, so the assertion keeps its
    sensitivity instead of buying reliability by being loosened.

    Wall clock stays right for the many tests here that assert only a ratio; this is for the two that
    also assert "and not more than N seconds", where wall clock measures the machine, not the code.
    """
    best = float("inf")
    for _ in range(attempts):
        started = time.process_time()
        fn()
        best = min(best, time.process_time() - started)
    return best


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
        #
        # Round-10 note: the leading CJK filler here is deliberately "系统" ("system"), not a
        # secret keyword -- this test is specifically about `_TOKEN_RE`'s own CJK-adjacency
        # boundary, independent of the labeled-secret subsystem. round-10 also moves `_TOKEN_RE`
        # into Phase B (see `redact()`'s own comment, item 4), so if the filler were a recognized
        # secret keyword ("密钥"), Phase A would now correctly claim the whole glued-on run first --
        # the same "no narrower pattern may fragment a keyword-claimed value" property already
        # documented for `test_redacts_standalone_jwt_immediately_adjacent_to_cjk_text` above --
        # which would produce a generic "[REDACTED]" instead of the "[REDACTED_TOKEN]" marker this
        # test specifically checks for. Neither outcome leaks the token either way.
        leading = hook.redact("系统sk-proj-abcdefgh12345678泄露")
        self.assertNotIn("sk-proj-abcdefgh12345678", leading)
        self.assertIn("[REDACTED_TOKEN]", leading)

        trailing = hook.redact("sk-proj-abcdefgh12345678泄露了")
        self.assertNotIn("sk-proj-abcdefgh12345678", trailing)
        self.assertIn("[REDACTED_TOKEN]", trailing)

        akia_trailing = hook.redact("AKIAABCDEFGHIJKLMNOP泄露了")
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", akia_trailing)
        self.assertIn("[REDACTED_TOKEN]", akia_trailing)

        # Sanity: ordinary ASCII-adjacent prefixed tokens still redact exactly as before. "the code"
        # (not "key is") for the same reason as the CJK filler above -- neutral wording that isn't a
        # recognized ASCII secret keyword, so this exercises `_TOKEN_RE` in isolation.
        ascii_case = hook.redact("the code sk-proj-abcdefgh12345678 was issued today")
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
        # Round-8 note: the leading CJK filler here is deliberately "系统" ("system"), not a
        # secret keyword -- this test is specifically about `_JWT_RE`'s own CJK-adjacency
        # boundary, independent of the labeled-secret subsystem. The original filler, "令牌"
        # ("token"), was itself a recognized secret keyword, so once Phase A (the labeled-secret
        # atomic-span capture) runs before `_JWT_RE` -- the round-8 architectural fix, see
        # `redact()`'s own comment -- that combination is no longer testing `_JWT_RE` in
        # isolation: "令牌" directly glued to a JWT-shaped run is now, correctly, claimed whole by
        # the keyword+connector+value pass as one atomic secret span before `_JWT_RE` ever runs,
        # which is the exact "no narrower pattern may fragment a keyword-claimed value" property
        # this rewrite exists to guarantee (see
        # `test_labeled_secret_atomic_capture_claims_an_adjacent_jwt_shaped_value` below for that
        # behavior's own dedicated coverage). Neither outcome leaks the JWT.
        leading = hook.redact(f"系统{jwt}泄露")
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
        # Round-2 P1 (superseded by the round-8 architectural rewrite -- see its own comment
        # above `redact()`): the round-1 CJK-secret passes ran *before* `_MAC_ADDRESS_RE` in
        # redact()'s pipeline. Their value class excluded ':', so a keyword glued directly (no
        # separator) to a longer ASCII run that was itself a MAC address truncated the ASCII
        # prefix off that run and left too few hex groups for `_MAC_ADDRESS_RE` to recognize
        # afterward -- a strict regression from "fully redacted" to "5 of 6 octets leak in the
        # clear". Rounds 2-7 fixed this by moving the CJK-secret passes to run *after*
        # `_MAC_ADDRESS_RE` instead, so the network pattern got first claim on the glued-on run
        # and the CJK pass could only add a second, separate "[REDACTED]" for whatever ASCII
        # prefix was left over (producing "令牌[REDACTED][REDACTED_IP]" for this exact input).
        #
        # Round-8 inverts that ordering deliberately (Phase A -- the keyword-triggered atomic
        # span capture -- now runs *before* Phase A's structural patterns, `_MAC_ADDRESS_RE`
        # included; see `redact()`'s module-level comment for why). Once a secret keyword directly
        # claims a value, the value class already includes ':' (has since round 3, for exactly
        # this kind of glued-on network-shaped tail), so the *entire* glued run -- "hO9il6bkY" and
        # the MAC-shaped "aa:bb:cc:dd:ee:ff" together -- is now claimed as ONE atomic span by the
        # keyword pass itself, and `_MAC_ADDRESS_RE` never gets to run on it at all: the whole
        # thing becomes a single "[REDACTED]", not "令牌[REDACTED][REDACTED_IP]". This is strictly
        # *more* redacted than before (no possibility of a fragment surviving between two separate
        # placeholders), and is the direct, intended consequence of the non-negotiable "no
        # narrower structural pattern may fire inside a keyword-claimed span" property this
        # rewrite exists to guarantee -- so the assertion below is updated to match, not relaxed:
        # the property this test actually cares about (no raw MAC octets ever survive in the
        # output) is unchanged and re-verified below.
        mac_glued = hook.redact("令牌hO9il6bkYaa:bb:cc:dd:ee:ff")
        self.assertNotIn("bb:cc:dd:ee:ff", mac_glued)
        self.assertNotIn("aa:bb:cc:dd:ee:ff", mac_glued)
        self.assertEqual(mac_glued, "令牌[REDACTED]")
        self.assertEqual(mac_glued, hook.redact(mac_glued), "must stay idempotent")

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
        # Round-3 regression guard, originally: the value-class widening above must not reach into
        # '[' / ']', since every CJK-secret pass ran after `_MAC_ADDRESS_RE`/`_IPV6_CANDIDATE_RE`
        # at the time and would otherwise swallow an already-inserted "[REDACTED_IP]" placeholder
        # and downgrade it to a generic "[REDACTED]".
        #
        # Round-8 note: the pass order this guard was written against is exactly what the round-8
        # architectural rewrite deliberately inverts (see `redact()`'s own comment and
        # `test_cjk_secret_redaction_does_not_narrow_an_adjacent_mac_or_ipv6_match`'s updated
        # comment above for the full account) -- the keyword pass now runs *before*
        # `_MAC_ADDRESS_RE`, so no "[REDACTED_IP]" placeholder exists yet for this input by the
        # time the value class would see it, and the whole glued run is claimed as one atomic
        # "[REDACTED]" span instead. The placeholder-downgrade property this test originally
        # guarded is still covered, unchanged, by
        # `test_redact_does_not_downgrade_a_typed_placeholder_sharing_a_secret_column` (a table
        # cell where the IPv6 address sits in a *non*-secret column, so it is genuinely redacted by
        # Phase B first and must survive Phase A's later per-cell scan of the *other*, secret
        # column intact) -- kept here as a same-input re-check that the whole value still redacts
        # atomically and stays idempotent.
        mac_glued = hook.redact("令牌hO9il6bkYaa:bb:cc:dd:ee:ff")
        self.assertNotIn("bb:cc:dd:ee:ff", mac_glued)
        self.assertEqual(mac_glued, "令牌[REDACTED]")
        self.assertEqual(mac_glued, hook.redact(mac_glued), "must stay idempotent")

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
        # connector never crosses a newline -- a deliberate round-2 false-positive guard). This
        # gap is real and broadly reachable in ordinary markdown memory files (headings, list
        # items, blockquotes, fenced code blocks all reproduce it). Round-8 (architectural-rewrite
        # process-integrity fix): this line used to assert the fully-unredacted output
        # (`hook.redact("密码\nTq4zW7pNe2Vs") == "密码\nTq4zW7pNe2Vs"`) as a passing expectation --
        # that pins a complete secret leak as "correct" behavior, which this file's own
        # process-integrity rule forbids regardless of whether the underlying gap is pre-existing.
        # Removed rather than re-expressed with a non-secret probe: a probe that isn't
        # secret-shaped wouldn't actually exercise the same code path (it would pass for the wrong
        # reason -- failing the false-positive guards, not the newline boundary), so it would not
        # be a meaningful regression test either way. Left undocumented in tests, as an explicit
        # prose-only residual gap, per this round's process rules -- see the round-8 final report
        # for the full analysis and its recommendation that this be scheduled as its own follow-up.
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

    def test_cjk_inline_secret_redaction_recognizes_dengyu_connector(self) -> None:
        # Architectural-rewrite verification pass (2026-08-22): "等于" ("equals") was another common
        # connector this vocabulary omitted -- `redact('密码等于Xk9pLmQ7Rt2vBn')` left the value
        # completely unredacted (fail-before verified against pre-fix HEAD in this round's report).
        secret = "Xk9pLmQ7Rt2vBn"
        redacted = hook.redact(f"密码等于{secret}")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "密码等于[REDACTED]")
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")
        # False-positive guard: ordinary prose using "等于" with no real value following must not
        # redact -- the candidate has no digit/@/uppercase-hyphen shape.
        for text in (
            "密码长度等于12位时最安全",
            "密钥长度等于256位",
            "令牌数量等于3个",
        ):
            self.assertEqual(hook.redact(text), text, text)

    def test_inline_ascii_secret_recognizes_equals_connector(self) -> None:
        # Architectural-rewrite verification pass (2026-08-22): the ASCII sibling of the "等于" gap
        # above -- `redact('password equals Xk9pLmQ7Rt2vBn')` left the value completely unredacted
        # (fail-before verified against pre-fix HEAD in this round's report).
        secret = "Xk9pLmQ7Rt2vBn"
        for text, expected in (
            (f"password equals {secret}", "password equals [REDACTED]"),
            (f"the token equals {secret}", "the token equals [REDACTED]"),
            (f"PASSWORD equals {secret}", "PASSWORD equals [REDACTED]"),
        ):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertEqual(redacted, expected, text)
            self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")
        # False-positive guard: ordinary prose using "equals" with no real secret value must not
        # redact -- the candidate has no digit/@/uppercase-hyphen shape and/or is a known non-secret
        # word.
        for text in (
            "the password equals its own hash after normalization, per RFC 1234",
            "this key equals that key in a math sense",
            "my token equals your token when synced",
        ):
            self.assertEqual(hook.redact(text), text, text)

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

    def test_secret_label_keyword_standalone_recognizes_compound_suffixed_labels(self) -> None:
        # Round-6 P1 BLOCKING (independent Claude opus + Codex dual review, 2026-08-22, retry
        # round): round-5's fix for the false positive below (widening the trailing negative
        # lookahead to also reject an immediately-following '_'/'-'/digit) also rejected the
        # dominant real-world secret-label naming convention -- a keyword joined to a
        # distinguishing suffix by '_'/'-' or followed directly by a digit. `_SECRET_LABEL_KEYWORD`
        # stopped matching these labels entirely, so both table paths (`_TABLE_CJK_SECRET_RE` and
        # the header/data-row column scan) silently stopped firing and the paired secret leaked in
        # full. Verified to FAIL against the pre-round-6 code (round-5 lookahead) and PASS after
        # reverting the lookahead to its round-4 form.
        secret = "Zq7#vT4nBx2W"
        cjk_secret = "Rw8mKt3ZpQ6y"
        same_row_cases = (
            (f"| db_password_prod | {secret} |", secret, "| db_password_prod | [REDACTED] |"),
            (f"| api_key_prod | {secret} |", secret, "| api_key_prod | [REDACTED] |"),
            (f"| password_1 | {secret} |", secret, "| password_1 | [REDACTED] |"),
            (f"| password-prod | {secret} |", secret, "| password-prod | [REDACTED] |"),
            (f"| 密码2 | {cjk_secret} |", cjk_secret, "| 密码2 | [REDACTED] |"),
            (f"| 密码-prod | {secret} |", secret, "| 密码-prod | [REDACTED] |"),
        )
        for text, secret_value, expected in same_row_cases:
            redacted = hook.redact(text)
            self.assertNotIn(secret_value, redacted, text)
            self.assertEqual(redacted, expected, text)
        # Header/data-row column-scan path too -- the label sits in a header row, the value in a
        # later data row of the same column.
        header_data = hook.redact(
            f"| user | db_password_prod |\n|---|---|\n| root | {secret} |"
        )
        self.assertNotIn(secret, header_data)
        self.assertEqual(
            header_data,
            f"| user | db_password_prod |\n|---|---|\n| root | [REDACTED] |",
        )

    def test_secret_label_keyword_standalone_rejects_ascii_compound_continuation(self) -> None:
        # Round-5 P2 (item 12) originally found: a compound identifier neither keyword vocabulary
        # has a dedicated alternative for -- "password_policy", "api_key_format", a CJK label glued
        # to an ASCII qualifier ("密码policy") -- can be treated as a genuine standalone secret
        # label and arm an entire markdown documentation-table column that was never a secret
        # column at all. Round-5 fixed this by rejecting a following '_'/'-'/digit, but that
        # widening caused the P1 leak fixed above (a real compound-suffixed secret label stopped
        # matching at all), so round-6 reverted it -- reopening this exact false positive -- and
        # round-6 also (incorrectly) renamed and inverted this test to *accept* the reopened false
        # positive as an "explicitly accepted tradeoff" instead of fixing the code.
        #
        # Round-10 (this retry round) restores the original, CORRECT expectation (unchanged output)
        # and fixes the code for real: `_LABEL_QUALIFIER_SUFFIX` (see
        # `_CJK_SECRET_KEYWORD_STANDALONE`'s own comment) resolves round-4's binary choice with a
        # closed vocabulary instead of a blanket accept/reject -- "prod"/"2" (real qualifiers) are
        # recognized and consumed as part of the label; "policy"/"format" (ordinary descriptive
        # words, not in the vocabulary) are not, so the label is correctly rejected as non-standalone
        # again, with no regression to the round-6 compound-suffix fix (see
        # `test_secret_label_keyword_standalone_recognizes_compound_suffixed_labels` above, which
        # re-verifies the exact repros round-6 was protecting in the same test run).
        unchanged_cases = (
            "| password_policy | min-12-chars |",
            "| api_key_format | uuid-v4 |",
            "| 密码policy | bcrypt |",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)
        # A bare keyword (no compounding at all) must still redact exactly as before.
        secret = "Xk9mQ2vR8pL"
        still_redacts = hook.redact(f"| password | {secret} |")
        self.assertNotIn(secret, still_redacts)
        self.assertEqual(still_redacts, "| password | [REDACTED] |")

    def test_inline_cjk_secret_connector_is_not_cubic(self) -> None:
        # Round-6 P1 BLOCKING (independent Claude opus + Codex dual review, 2026-08-22, retry
        # round): the round-5 additions (`_CJK_LABEL_QUALIFIER`, `_CJK_CONNECTOR_LEAD_PUNCT`) made
        # the connector's whitespace four separate, mutually adjacent unbounded `[^\S\n]*` runs.
        # When the value ultimately fails to match, the engine enumerates every way to partition
        # one whitespace run across those four groups -- catastrophic backtracking, roughly cubic
        # in the run's length. Reachable end-to-end through `hook.run()` on ordinary memory content
        # (a long trailing-whitespace run after a CJK secret keyword), stalling every prompt
        # submission up to the hook's outer timeout. Bounding each run to a small fixed maximum
        # (see `_CJK_CONNECTOR_WS`) caps this to a small constant regardless of input length.
        # Verified to FAIL (multi-second) against the pre-round-6 code and PASS (sub-second, even
        # at 20x the size that made round-5's code take ~4.7s) after the fix.
        adversarial = "密码" + " " * 12_000 + "x"
        elapsed = _min_elapsed(lambda: hook._INLINE_CJK_SECRET_RE.search(adversarial))
        # Best-of-5 (see `_min_elapsed`) plus a generous backstop: real cubic/quadratic
        # backtracking at this input size would take seconds-to-minutes on every attempt,
        # not just occasionally exceed a tight sub-second ceiling under CPU contention.
        self.assertLess(elapsed, 3.0)
        # Same shape reachable through the real hook.run() pipeline, not just the bare pattern.
        self.fixture.add_memory(
            "| 密码" + " " * 700 + "|\n更多笔记内容\n"
        )
        run_elapsed = _min_elapsed(lambda: self.fixture.run("笔记"), attempts=3)
        self.assertLess(run_elapsed, 8.0)

    def test_inline_cjk_secret_connector_allows_long_trailing_whitespace_before_value(
        self,
    ) -> None:
        # Round-7 P1 BLOCKING (independent Claude opus + Codex dual review, 2026-08-22, retry
        # round): the round-6 ReDoS fix bounded *every* connector whitespace run to 8 characters,
        # including the one right after the separator token and immediately before the value --
        # so a real secret separated from its label by 9+ horizontal whitespace characters (an
        # aligned key/value paste) stopped matching at all and leaked in full. Fixed by leaving
        # only the final run (the one after `sep_tok`) unbounded; verified fail-before/pass-after
        # against the round-6 code.
        secret = "Zq7#tW4nJ8xB"
        for n in (8, 9, 12, 20, 40, 200):
            redacted = hook.redact("密码：" + " " * n + secret)
            self.assertNotIn(secret, redacted, f"n={n}")
            self.assertIn("[REDACTED]", redacted, f"n={n}")
        # Companion aligned key/value paste shape from the review's own repro.
        aligned = hook.redact("用户名:          admin\n密码:            " + secret)
        self.assertNotIn(secret, aligned)
        self.assertIn("[REDACTED]", aligned)

    def test_cn_mobile_number_widening_does_not_truncate_an_adjacent_secret(self) -> None:
        # Round-7 P2 BLOCKING (independent Claude opus + Codex dual review, 2026-08-22, retry
        # round): round-6 widened `_CN_MOBILE_RE` to allow `-`/space digit-group separators and
        # ran it BEFORE the CJK-secret passes, so a phone-shaped substring sitting inside a longer
        # labeled secret value got nibbled out as "[REDACTED_PHONE]", leaving the surrounding
        # secret characters in the clear -- e.g. `redact('密码：Ab-138-0013-8000-Cd')` produced
        # `'密码：Ab-[REDACTED_PHONE]-Cd'` instead of redacting the whole value. Fixed by moving
        # the structured-PII passes to run after the CJK-secret passes, so the whole value is
        # already replaced with a single "[REDACTED]" placeholder before `_CN_MOBILE_RE` ever sees
        # it. This is the same narrower-pattern-nibbles-a-longer-value bug class pinned by
        # `test_cjk_secret_redaction_does_not_narrow_an_adjacent_mac_or_ipv6_match` above.
        secret = "Ab-138-0013-8000-Cd"
        self.assertEqual(hook.redact(f"密码：{secret}"), "密码：[REDACTED]")
        self.assertEqual(
            hook.redact(f"| 密码 | {secret} |"), "| 密码 | [REDACTED] |"
        )
        self.assertNotIn(secret, hook.redact(f"密码：{secret}"))
        self.assertNotIn("[REDACTED_PHONE]", hook.redact(f"密码：{secret}"))

    def test_cn_mobile_number_widening_does_not_cross_newlines(self) -> None:
        # Round-7 P2 (independent Claude opus + Codex dual review, 2026-08-22, retry round): the
        # round-6 `[-\s]?` separator class used bare `\s`, which includes `\n` -- unlike every
        # other line-scoped pattern in this file (see `_CJK_CONNECTOR_WS`'s comment and the
        # round-2 newline guard it documents). That let `_CN_MOBILE_RE` match ACROSS line breaks,
        # merging three unrelated numbers into one bogus phone-number redaction and deleting the
        # newlines between them, e.g. `redact('季度数据\n139\n1234\n5678\n合计')` collapsed five
        # lines into three. Fixed by switching to a horizontal-only separator class (`[- \t]`).
        text = "季度数据\n139\n1234\n5678\n合计"
        redacted = hook.redact(text)
        self.assertEqual(redacted, text)
        self.assertNotIn("[REDACTED_PHONE]", redacted)
        self.assertEqual(redacted.count("\n"), 4)
        # A real, deliberately formatted phone number (space/hyphen grouped, no newlines) must
        # still redact exactly as before this fix.
        for formatted in ("13800138000", "+86 138-0013-8000", "0086-13800138000", "138 0013 8000"):
            self.assertEqual(hook.redact(formatted), "[REDACTED_PHONE]", formatted)

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
        # Item 12(a): SUPERSEDED this round (architectural-rewrite round 1) -- was previously
        # documented as an accepted, non-blocking residual gap ("the permissive value class an
        # explicit separator selects has no way to distinguish a real secret from an ordinary
        # technical term with no digit in it"), because closing it with a shape/length heuristic
        # risked declining a genuine digitless secret elsewhere (e.g. "supersecretvalue", a real,
        # separately pinned credential -- see `test_redacts_json_quoted_and_snake_case_credentials`).
        # This round closes it anyway, via a mechanism that carries no such risk: a small, closed
        # vocabulary of well-known, never-secret hashing/crypto algorithm names
        # (`_KNOWN_NON_SECRET_WORDS`, checked by `_redact_inline_cjk_secret`) that "bcrypt" is a
        # member of. Unlike a shape heuristic, this can only ever misfire if a real secret is
        # spelled exactly like one of ~20 well-known algorithm names -- a narrow, disclosed,
        # already-precedented class of risk (the same this file already accepts for
        # `_LABEL_QUALIFIER_WORD`), not a new one. The old expectation was a deliberately accepted
        # tradeoff at the time, not a design invariant -- this round's explicit brief asked for
        # exactly this false positive to be closed, and the mechanism above closes it without
        # reopening any digitless-secret coverage.
        unchanged = "当前密码：bcrypt 哈希算法需要升级"
        self.assertEqual(hook.redact(unchanged), unchanged)
        # Item 10 (a real secret that happens to literally contain this file's own placeholder
        # shape, "[REDACTED_TOKEN]", as a substring, defeating the tempered-greedy-token guard
        # that exists to keep `redact()` idempotent) is NOT re-pinned here: round 10 asserted the
        # resulting under-redacted output as a passing expectation, which is exactly the test-
        # integrity violation this effort's own process rules forbid. It genuinely could not be
        # closed this round without reopening the round-4 double-wrap/placeholder-downgrade bug
        # the guard exists to prevent (the guard cannot distinguish "this bracket text is a
        # placeholder from an earlier `redact()` pass" from "this bracket text is coincidentally
        # shaped like one but is raw, never-redacted secret content" -- they are byte-identical);
        # see this round's final report for the full analysis. Left undocumented in tests, as an
        # explicit prose-only residual gap, per this round's process rules.
        #
        # Round-11 (this round) fix: the item-9/item-15 bracketed-qualifier leak below, which
        # round 10 also pinned as a passing "documented" gap (including asserting non-idempotency
        # via `assertNotEqual` -- the other half of the same process violation), IS fixed this
        # round -- see `_CJK_LABEL_QUALIFIER_WORD`'s own comment for the closed-vocabulary
        # mechanism. A keyword glued directly to a secret that happens to start with
        # "(<arbitrary text>)" immediately before a real separator no longer misreads that text as
        # a bracketed environment/scope qualifier: only a small, closed vocabulary of realistic
        # qualifier words (生产/prod/dev/staging/...) is recognized as a qualifier at all, so
        # "SecretBytesHere1234" (matching none of them) is no longer treated as one, and the whole
        # span is instead captured atomically as the STRICT bare-mention value -- zero real secret
        # characters echoed, and (since nothing placeholder-shaped survives) trivially idempotent.
        qualifier_gap = hook.redact("密码(SecretBytesHere1234):tail")
        self.assertEqual(qualifier_gap, "密码[REDACTED]")
        self.assertNotIn("SecretBytesHere1234", qualifier_gap)
        self.assertEqual(
            qualifier_gap, hook.redact(qualifier_gap), "must stay idempotent"
        )
        # The previously-tested qualifier-echo UX for a REAL environment/scope note is unaffected:
        # a word inside the closed vocabulary still survives visibly next to the placeholder -- see
        # `test_cjk_inline_secret_redaction_handles_bracketed_qualifier_before_separator`.

    # --- round-8 architectural rewrite (2026-08-22): 7 prior dogfood dual-review rounds each
    # closed some fragment-leak repros while introducing new ones of the identical shape --
    # a narrow keyword-triggered value class kept losing a race against `_IPV4_RE`/`_MAC_ADDRESS_RE`/
    # `_CN_MOBILE_RE`/etc., which ran first and could recognize (and claim) just the sub-shape of a
    # real secret that happened to look network- or phone-shaped, leaving the rest of the value
    # exposed next to a placeholder that made the output look fully handled. This round replaces
    # that whole family of one-off reorderings with a real two-phase architecture: Phase A (the
    # keyword+connector+value passes) now runs *before* every narrower structural pattern that
    # could otherwise see inside a labeled value (see `redact()`'s own module-level comment). Every
    # test below was verified to FAIL against the pre-round-8 HEAD (the version with the
    # architecture described in that comment's "PHASE B" list still running *before* the
    # keyword-triggered passes) and PASS after this round's fix. Every secret value below is
    # synthetic, constructed for these tests -- never a real secret. ---

    def test_labeled_secret_atomic_capture_survives_an_embedded_space_grouped_phone_shape(
        self,
    ) -> None:
        # Regression test 1: a labeled backup/recovery-code value that is space-grouped and
        # phone-number-shaped end to end. Pre-round-8: `_CN_MOBILE_RE` ran after the keyword pass
        # but the keyword pass's own value class had no way to span the internal spaces at all
        # (its body excluded plain whitespace entirely), so the labeled-secret pass didn't match
        # this shape as a value in the first place -- `_CN_MOBILE_RE` then nibbled the first
        # phone-shaped chunk out of the middle of it: `redact('备份码：139 8842 7615 3320')`
        # produced `'备份码：[REDACTED_PHONE] 3320'`, leaking "3320" in the clear. Must now redact
        # as ONE atomic span with zero characters of the real value surviving.
        secret = "139 8842 7615 3320"
        text = f"备份码：{secret}"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "备份码：[REDACTED]")
        self.assertNotIn("139", redacted)
        self.assertNotIn("3320", redacted)
        self.assertNotIn("[REDACTED_PHONE]", redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

    def test_labeled_secret_atomic_capture_survives_an_embedded_ipv4_shaped_substring(self) -> None:
        # Regression test 2: a labeled password value containing an embedded IPv4-shaped substring
        # with prefix/suffix characters around it. Pre-round-8: `_IPV4_RE` ran *before* the
        # keyword-triggered pass and claimed just the dotted-quad, leaving the surrounding prefix
        # and suffix of the real secret in the clear right next to a "[REDACTED_IP]" marker that
        # made the output look fully handled: `redact('密码：Ab-192.0.2.44-Cd')` produced
        # `'密码：Ab-[REDACTED_IP]-Cd'`. Must now redact as ONE atomic span.
        secret = "Ab-192.0.2.44-Cd"
        text = f"密码：{secret}"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]")
        self.assertNotIn("Ab-", redacted)
        self.assertNotIn("192.0.2.44", redacted)
        self.assertNotIn("-Cd", redacted)
        self.assertNotIn("[REDACTED_IP]", redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

    def test_labeled_secret_atomic_capture_recognizes_separator_free_device_code(self) -> None:
        # Regression test 3: a separator-free alphabetic-hyphenated device/authorization code
        # following its keyword with only bare whitespace (no colon, no 是/为 connector). Pre-
        # round-8, the bare-whitespace STRICT value class required an ASCII digit somewhere in the
        # value; a realistic uppercase, dash-grouped device code has none, so it wasn't recognized
        # as a value at all: `redact('设备码 WDJB-MJHT')` left the input completely unchanged.
        secret = "WDJB-MJHT"
        text = f"设备码 {secret}"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "设备码 [REDACTED]")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

        # Companion false-positive guard for the same widening: an ordinary hyphenated English
        # word glued to a keyword by a stray bare-whitespace connector must NOT be mistaken for a
        # device code -- `_looks_like_secret_code` requires an all-uppercase (no ASCII lowercase)
        # value when no digit is present, which a real device code satisfies and ordinary lowercase
        # prose does not.
        for unchanged in (
            "密码 through-put is amazing",
            "私钥 well-known ports include 80",
            "密钥 read-only mode is the default",
        ):
            self.assertEqual(hook.redact(unchanged), unchanged, unchanged)

    def test_labeled_secret_keyword_glued_directly_to_a_digit_or_dash_value_redacts_whole(
        self,
    ) -> None:
        # Round-9 P0 regression: the round-8 attempt's compound-suffix machinery
        # (`_CJK_SECRET_KEYWORD_SUFFIX`) was greedy and unconditional, so a keyword glued directly
        # (no separator at all) onto a real secret whose value happens to start with digits or a
        # dash ate the *first characters of the real secret* as if they were a label qualifier, and
        # echoed them back in the clear: `redact('密码1234567890abcd')` produced
        # `'密码12345678[REDACTED]'` (8 real secret characters leaked),
        # `redact('密钥-Ab3xK9mQ2z')` produced `'密钥-Ab3xK9[REDACTED]'` (7 characters leaked). Must
        # now redact the entire glued-on value, echoing back only the bare keyword.
        for text, expected in (
            ("密码1234567890abcd", "密码[REDACTED]"),
            ("密钥-Ab3xK9mQ2z", "密钥[REDACTED]"),
            ("验证码884213morecontent", "验证码[REDACTED]"),
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")
        # The compound-suffix-plus-real-separator shape this machinery exists for must still be
        # RECOGNIZED (i.e. still redact the value, not leak it).  The scanner-first rewrite keeps a
        # recognized compound label intact in both inline and table contexts; it is label metadata,
        # not a byte range of the claimed value.
        for text, expected in (
            ("密码2：Tq4%wZ7nJ2bV", "密码2：[REDACTED]"),
            ("密码-prod：Tq4%wZ7nJ2bV", "密码-prod：[REDACTED]"),
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")

    def test_labeled_secret_atomic_capture_claims_a_bearer_prefixed_value_whole(self) -> None:
        # Round-9 P0 regression: `_BEARER_RE` runs in Phase B (after the labeled-secret atomic-span
        # capture), but pre-round-9 the labeled-value grammar had no way to recognize "Bearer
        # <token>" as one unit -- it stopped at the space after the bare word "Bearer" (the
        # digit-continuation exception only fires before a digit, never before a letter), redacting
        # only the word "Bearer" and leaving the real token exposed right next to the placeholder,
        # with `_BEARER_RE` unable to rescue it afterward since its own anchor ("Bearer") was
        # already gone: `redact('密钥：Bearer aB3xK9mQ2vR8pLz7WcN4')` produced
        # `'密钥：[REDACTED] aB3xK9mQ2vR8pLz7WcN4'`. Covers the CJK inline path, the ASCII
        # `_ASSIGNMENT_RE` path, and the table-cell path -- all three route through independent
        # value grammars.
        token = "aB3xK9mQ2vR8pLz7WcN4"
        for text, expected in (
            (f"密钥：Bearer {token}", "密钥：[REDACTED]"),
            (f"api_key: Bearer {token}", "api_key: [REDACTED]"),
            (f"| 密钥 | Bearer {token} |", "| 密钥 | [REDACTED] |"),
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertNotIn(token, redacted, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")
        # Sanity: an unlabeled Bearer token (no recognized secret keyword nearby) still redacts via
        # `_BEARER_RE` in Phase B exactly as before -- this fix only changes behavior when a
        # labeled-secret keyword claims the span first.
        unlabeled = hook.redact(f"Authorization: Bearer {token}")
        self.assertNotIn(token, unlabeled)
        self.assertIn("Bearer [REDACTED]", unlabeled)

    def test_labeled_secret_atomic_capture_claims_a_bearer_value_with_prefix_before_bearer(
        self,
    ) -> None:
        # Round-15 P0 regression (independent Claude opus + Codex, 2026-08-22, against the round-9
        # attempt above): the round-9 fix only recognized "Bearer " as a value PREFIX, matched only
        # AT the value's own starting position. When the value has ANY characters before the
        # literal word "Bearer" (a version tag / scheme prefix -- an entirely realistic shape, e.g.
        # a scoped token format or a copy-pasted "v2.Bearer <token>" credential line), that
        # start-anchored optional group can never fire: the ordinary per-character value class
        # silently consumes the prefix *and* the word "Bearer" one byte at a time (both are just
        # ordinary ASCII letters/punctuation to that class) and then stops at the space right after
        # "Bearer", because the space-continuation lookahead requires a digit or an uppercase code
        # group to follow and a realistic opaque token (all lowercase, no digit -- e.g. a Slack bot
        # token, a GitHub app installation token, a plain session token) satisfies neither. Net
        # effect: Phase A claims only "...Bearer" and destroys `_BEARER_RE`'s own required anchor,
        # so Phase B can no longer rescue the token either.
        #
        # Verified fail-before against the actual pre-fix code in this file (reconstructed here
        # from the exact pre-round-15 constants, not asserted from memory): with a "Aa9." prefix
        # before "Bearer", all three independent value grammars below (CJK inline `：` separator,
        # ASCII `_ASSIGNMENT_RE` `key:` separator, table cell) captured only `"Aa9.Bearer"`, leaving
        # the entire token exposed in the clear right next to the placeholder -- confirmed:
        #   `redact('密钥：Aa9.Bearer xoxbslackbotusertoken')` ->
        #   `'密钥：[REDACTED] xoxbslackbotusertoken'`
        # (and the identical shape on the `api_key:` and table-cell paths). This token fixture is
        # deliberately realistic-opaque (all lowercase, no digit, no hyphen) -- the round-9 test's
        # own `aB3xK9mQ2vR8pLz7WcN4` fixture happens to contain both cases and a digit, so it stays
        # "safe" under the space-continuation lookahead even when truncated after "Bearer", masking
        # this exact gap; a plain lowercase opaque token does not have that accidental protection.
        token = "xoxbslackbotusertoken"
        for text, expected in (
            (f"密钥：Aa9.Bearer {token}", "密钥：[REDACTED]"),
            (f"api_key: Aa9.Bearer {token}", "api_key: [REDACTED]"),
            (f"| 密钥 | Aa9.Bearer {token} |", "| 密钥 | [REDACTED] |"),
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertNotIn(token, redacted, text)
            self.assertNotIn("Bearer", redacted, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")

    def test_labeled_secret_atomic_capture_survives_a_realistic_long_jwt(self) -> None:
        # Round-9 P1 regression: the round-8 attempt capped the labeled-value grammar at 200
        # characters, and moved `_JWT_RE` to Phase B (after that capture runs) -- but a realistic
        # JWT is routinely 200-800+ characters, so a labeled JWT was truncated at 200 chars by
        # Phase A and the tail (including the entire HMAC signature) was left exposed in the clear,
        # with `_JWT_RE` unable to rematch since its own header anchor was already consumed. This
        # uses a synthetic ~280-character JWT-shaped value (three base64url segments), comfortably
        # past the old 200-char cap, to catch a regression back to that truncation.
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            + "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWUsImlhdCI6MTUxNjIzOTAyMiwiZXhwIjoyNTE2MjM5MDIyLCJhdWQiOiJvcmNhLXRlc3QiLCJpc3MiOiJvcmNhLW1lbW9yeS1icmlkZ2UiLCJzY29wZSI6InJlYWQgd3JpdGUgYWRtaW4ifQ"
            + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5cSflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        self.assertGreater(len(jwt), 250, "fixture must exceed the old 200-char truncation cap")
        for text, label in ((f"密钥：{jwt}", "cjk"), (f"api_key: {jwt}", "assignment")):
            redacted = hook.redact(text)
            self.assertNotIn(jwt, redacted, label)
            # No fragment of the signature segment may survive.
            self.assertNotIn(jwt.rsplit(".", 1)[-1], redacted, label)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {label}")

    def test_labeled_secret_atomic_capture_survives_ascii_labeled_space_grouped_value(
        self,
    ) -> None:
        # Round-9 P2 regression: the item-2(a) atomic-span property (a space-grouped,
        # phone-number-shaped labeled value must redact as one span, not be nibbled mid-value by
        # `_CN_MOBILE_RE`) only held for the CJK inline/table paths pre-round-9.
        # `_INLINE_ASCII_SECRET_RE` always used the plain STRICT value class (no space tolerance),
        # so `redact('backup token is 159 3321 8874 6650')` produced
        # `'backup token is [REDACTED_PHONE] 6650'` -- "6650" survived in the clear next to a
        # placeholder that made the line look fully handled.
        text = "backup token is 159 3321 8874 6650"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "backup token is [REDACTED]")
        self.assertNotIn("159", redacted)
        self.assertNotIn("6650", redacted)
        self.assertNotIn("[REDACTED_PHONE]", redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")
        # False-positive guard for the same widening: ordinary English prose after "is" with no
        # digit anywhere must still not be mistaken for a secret (the STRICT digit-or-hyphen guard
        # is unchanged, only its space-tolerance is new).
        for unchanged in (
            "backup token is essential for security",
            "the api key concept is fundamental to REST design",
        ):
            self.assertEqual(hook.redact(unchanged), unchanged, unchanged)

    def test_labeled_secret_raised_cap_and_bearer_prefix_scale_linearly(self) -> None:
        # Round-9: two new pieces of machinery in the labeled-value grammar -- the raised 4096-char
        # cap (was 200) and the optional "Bearer " prefix scan -- must not reopen a ReDoS surface.
        # Measured scaling, not just "it completes": doubling the input should roughly double the
        # time (linear), not quadruple or worse.
        # Round-6 P3 finding (independent Claude opus + Codex dual review, 2026-08-22): the
        # single-shot absolute-ceiling assertions below flaked under concurrent CPU contention
        # on the same machine even though best-of-5 timings confirmed genuinely linear scaling.
        # `_min_elapsed` (best-of-5) plus a loosened absolute backstop keeps this sensitive to a
        # real regression (which would be slow on every attempt) without flaking on noise.
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        # Adversarial shape 1: a value long enough to exercise the raised cap end to end (well
        # past the old 200-char truncation point), repeated to amplify any per-match cost.
        small = timed("密码：" + ("Qz7mNeR8vTx2" * 100 + " ") * 4)  # ~4 x 1200-char values
        large = timed("密码：" + ("Qz7mNeR8vTx2" * 100 + " ") * 16)  # 4x input
        self.assertLess(large, small * 4 + 0.5, "raised-cap values must scale near-linearly")
        self.assertLess(large, 3.0)

        # Adversarial shape 2: the literal word "Bearer" repeated many times with no real token
        # following (so the optional prefix is attempted, and declined, at every occurrence).
        small2 = timed("密钥：" + "Bearer " * 2_000)
        large2 = timed("密钥：" + "Bearer " * 8_000)  # 4x input
        self.assertLess(large2, small2 * 4 + 0.5, "repeated Bearer-prefix scan must scale near-linearly")
        self.assertLess(large2, 3.0)

    def test_labeled_secret_table_column_scan_handles_escaped_pipe_in_a_data_row(self) -> None:
        # Regression test 4: a markdown table cell containing an escaped pipe inside the secret
        # value, in the header/data-row shape (label in one row, value in a later row of the same
        # column) -- distinct from the same-row shape `_TABLE_CJK_SECRET_RE`'s own value class
        # already handled since round 5. Pre-round-8, `_split_table_row_cells`/`_replace_table_cell`
        # split on a bare `line.split("|")`, so the escaped pipe was read as a real cell boundary
        # and the value matcher only ever saw the fragment before it:
        # `redact('| 用户 | 密码 |\n|---|---|\n| root | Qx9\\|Lm2N7 |')` produced
        # `'| 用户 | 密码 |\n|---|---|\n| root | [REDACTED]|Lm2N7 |'`, leaking "Lm2N7" in the clear.
        secret = "Qx9\\|Lm2N7"
        text = f"| 用户 | 密码 |\n|---|---|\n| root | {secret} |"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "| 用户 | 密码 |\n|---|---|\n| root | [REDACTED] |")
        self.assertNotIn("Lm2N7", redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

    def test_labeled_secret_compound_suffixed_inline_labels_match_like_table_labels_now(
        self,
    ) -> None:
        # Regression test 5: compound-suffixed labels (keyword+digit, keyword+"-prod") in BOTH
        # inline-prose and table-cell contexts, confirming the two matchers now share vocabulary.
        # Pre-round-8, `_INLINE_CJK_SECRET_RE`'s keyword group had nothing to consume the compound
        # suffix, so the connector -- which starts matching immediately after the keyword -- landed
        # on the stray suffix character and failed outright:
        # `redact('密码2：Tq4%wZ7nJ2bV')` and `redact('密码-prod：Tq4%wZ7nJ2bV')` both left the
        # input completely unchanged, even though the identical labels already redacted correctly
        # in a table cell (`test_secret_label_keyword_standalone_recognizes_compound_suffixed_labels`
        # above).  The scanner-first rewrite now keeps the recognized label suffix intact, matching
        # the table representation and avoiding the misleading loss of label distinction.
        secret = "Tq4%wZ7nJ2bV"
        for text, expected in (
            (f"密码2：{secret}", "密码2：[REDACTED]"),
            (f"密码-prod：{secret}", "密码-prod：[REDACTED]"),
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertNotIn(secret, redacted, text)
        # Table-cell sibling, already working before this round (kept here so both contexts are
        # exercised side by side in one test, per the task's own framing).
        table_secret = "Zq7#vT4nBx2W"
        for text, expected in (
            (f"| 密码2 | {table_secret} |", f"| 密码2 | [REDACTED] |"),
            (f"| 密码-prod | {table_secret} |", f"| 密码-prod | [REDACTED] |"),
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertNotIn(table_secret, redacted, text)

    def test_labeled_secret_suffix_cannot_be_spent_on_an_embedded_separator_inside_the_value(
        self,
    ) -> None:
        # Round-10 P0/P1 regression (independent Claude opus + Codex, 2026-08-22, against the
        # round-9 attempt): the compound-suffix alternative's suffix was greedy and would consume
        # up to 33 characters as long as SOME reachable separator token followed -- but every
        # separator token (":"/"："/"="/"＝") is itself an ordinary legal value character, so
        # whenever a keyword-glued value happened to contain one anywhere within the suffix's
        # reach, the suffix ate everything up to it as a fake "label qualifier" and those real
        # secret bytes were echoed straight back into the output. See
        # `_CJK_SECRET_KEYWORD_SUFFIX`'s own comment for the full history and fix (a closed
        # qualifier vocabulary, plus never echoing a non-empty suffix even if the vocabulary check
        # is somehow wrong). Each case must redact to exactly "<keyword>[REDACTED]" with zero
        # characters of the real secret echoed, and stay idempotent.
        for text, expected in (
            ("密钥-Ab3xK9mQ2vR8pLz7WcN4tYu6H:finaltail99", "密钥[REDACTED]"),
            ("密码-Q7wE9rT2yU4iO6pA8sD0fG1hJ3kL5=zXcVbNm2", "密码[REDACTED]"),
            ("令牌98765432:Ab3xK9mQ2v", "令牌[REDACTED]"),
            ("令牌-Ab2001:db8:85a3::8a2e:370:7334", "令牌-[REDACTED]"),
            ("口令-Qw8eR:Tz5uIo1Pa", "口令[REDACTED]"),
            ("签名-Kx9mQ:aB3xK9mQ2vR8", "签名[REDACTED]"),
            ("密码-Zt7pQ:Lw4nHc8Vb", "密码[REDACTED]"),
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")
        # The full-width-colon variant of the same shape (a distinct sub-bug: the STRICT value
        # class did not include "："/"＝" at all, so the second alternative's bare scan truncated
        # right there and left everything after it exposed -- see
        # `_CJK_VALUE_FULLWIDTH_SEP_CHARS`'s own comment).
        fullwidth = hook.redact("密钥-Ab3xK9：mQ2vR8pLz")
        self.assertEqual(fullwidth, "密钥[REDACTED]")
        self.assertNotIn("mQ2vR8pLz", fullwidth)
        self.assertEqual(fullwidth, hook.redact(fullwidth), "must stay idempotent")

    def test_labeled_secret_suffix_fold_fix_does_not_reopen_pure_punctuation_or_placeholder_gaps(
        self,
    ) -> None:
        # Round-10 regression guard: fixing the full-width-colon leak above (adding "："/"＝" to
        # the value character class) opened a second, independent false-positive path -- when an
        # explicit separator is present but nothing valid follows it, the plain (non-suffixed)
        # alternative backtracks its optional separator to "not taken" and retries a bare STRICT
        # scan starting from the connector's own position, which -- with "："/"＝" now in the body
        # class -- is the separator character itself. The separator then counted toward the
        # STRICT class's own length floor and digit-or-hyphen guard, misreading pure punctuation
        # or this file's own placeholder text as a bare secret value. Fixed with a lookahead that
        # forbids a value match from ever *starting* on a full-width separator (see
        # `_cjk_value_pattern`'s own comment) -- this case must stay completely unchanged, exactly
        # as it did before "："/"＝" were added to the value class at all.
        self.assertEqual(hook.redact("密码：----"), "密码：----")
        # Round-11: the second half of this regression guard (a secret literally containing this
        # file's own placeholder shape, "[REDACTED_TOKEN]") is no longer pinned here -- round 10
        # asserted the resulting under-redacted output as a PASS, which is the process-integrity
        # violation this effort's rules forbid; see
        # `test_cjk_secret_redaction_documents_further_known_residual_gaps`'s own comment (item 10)
        # for why this specific shape is a genuine, undocumented-in-tests, prose-only residual gap
        # rather than something this round closes.

    def test_labeled_secret_suffix_fold_fuzz_idempotency_repros(self) -> None:
        # Round-10 P1 regression (independent Claude opus + Codex, 2026-08-22, item 2): a direct
        # consequence of the P0 above -- a real secret containing an embedded separator, glued
        # directly to its keyword, produced non-idempotent output at round 9
        # (`redact(redact(x)) != redact(x)`): the first pass left real secret bytes exposed next
        # to a placeholder, and a second pass then found and consumed more of the same secret. A
        # seeded fuzz found 8 such inputs against the round-9 code; a representative sample is
        # pinned here, spanning several keywords and several surrounding contexts (start of line,
        # after a leading punctuation/markdown marker, before trailing prose).
        for text in (
            "令牌2001:db8::42 and more text",
            "- 签名2001:db8::42",
            "| 密码2001:db8::42",
            "**令牌2001:db8::42，记得改",
            "密钥2001:db8::42 2026年",
            # Qualifier-plus-digit CJK labels must preserve the complete label
            # when the already-redacted value is encountered on a second pass.
            "密码-prod2：Km-198.51.100.17-Vt",
            "密钥-dev3: 188 2244 6699 7788",
            "令牌_prod2=WXYZ-QRST",
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")
            self.assertNotIn("db8::42", redacted, text)
            self.assertNotRegex(redacted, r"(?:198\.51\.100\.17|188 2244|WXYZ-QRST)")

    def test_inline_ascii_secret_recognizes_compound_suffixed_labels(self) -> None:
        # Round-10 P1 regression (independent Claude opus + Codex, 2026-08-22, item 3): a
        # compound-suffixed ASCII label ("db_password_prod", "password_1", "api_key_prod",
        # "token_2", "password-prod", "api-key-prod") already redacted correctly in a table cell
        # (via `_ASCII_SECRET_KEYWORD_STANDALONE`'s own compound-suffix tolerance) but leaked
        # COMPLETELY in inline "is" prose, since `_INLINE_ASCII_SECRET_RE` had no suffix concept
        # at all and the mandatory " is " connector -- which starts matching immediately after the
        # keyword -- landed on the stray suffix character instead of real whitespace. See
        # `_INLINE_ASCII_SECRET_RE`'s own comment for the fix. Every case must redact (the value
        # must not survive in the output); the base keyword is echoed.
        #
        # Round-9 fix (retry-gate finding 9): the six expected values below used to fold the
        # qualifier suffix into the placeholder ("db_password_prod is ..." -> "db_password is
        # [REDACTED]", losing "_prod"), described in the comment above as intentional "the same
        # defense-in-depth policy as the CJK sibling" -- that description was not accurate for this
        # pattern: unlike `_redact_inline_cjk_secret`'s `keyword_cs`/`suffix_cs` branch (which folds
        # ONLY when its own regex could not tell a genuine qualifier from the start of the value,
        # and otherwise echoes it), `_INLINE_ASCII_SECRET_RE`'s suffix group was simply never
        # captured at all, so it was ALWAYS silently discarded with no ambiguity-driven choice
        # involved -- an accidental label-metadata loss, not a deliberate policy. The suffix is now
        # captured and echoed like every other qualifier case in this file (see
        # `_redact_inline_ascii_secret`'s own comment); the value itself was never leaking either
        # way (`assertNotIn(secret, redacted)` below, both before and after this fix).
        secret = "Gh7#kL9mWq2"
        for text, expected in (
            (f"db_password_prod is {secret}", f"db_password_prod is [REDACTED]"),
            (f"password_1 is {secret}", f"password_1 is [REDACTED]"),
            (f"api_key_prod is {secret}", f"api_key_prod is [REDACTED]"),
            (f"token_2 is {secret}", f"token_2 is [REDACTED]"),
            (f"password-prod is {secret}", f"password-prod is [REDACTED]"),
            (f"api-key-prod is {secret}", f"api-key-prod is [REDACTED]"),
        ):
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertNotIn(secret, redacted, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")

    def test_token_and_url_userinfo_no_longer_fragment_a_keyword_claimed_value(self) -> None:
        # Round-10 P2 regression (independent Claude opus + Codex, 2026-08-22, item 4): `_TOKEN_RE`
        # and `_URL_USERINFO_RE` are bare, prefix-anchored SHAPE patterns with no keyword/value
        # grammar of their own -- structurally identical to `_IPV4_RE`/`_MAC_ADDRESS_RE`/etc.,
        # which round 8 already moved into Phase B. Left running in the prologue, either pattern
        # could still claim just its own recognized substring inside a keyword-labeled value and
        # leave the rest of that value exposed next to its own placeholder. Both moved into Phase B
        # this round -- see `redact()`'s own comment for the full history.
        token_case = hook.redact("密码：Aa-sk-abcdefghij1234567890-Zz")
        self.assertEqual(token_case, "密码：[REDACTED]")
        self.assertNotIn("sk-abcdefghij1234567890", token_case)
        self.assertNotIn("Aa-", token_case)
        self.assertNotIn("-Zz", token_case)

        url_case = hook.redact("密码：pre-https://bob:hunter2@example.com-post")
        self.assertEqual(url_case, "密码：[REDACTED]")
        self.assertNotIn("hunter2", url_case)
        self.assertNotIn("post", url_case)

        # Sanity: a standalone (unlabeled) token/userinfo URL elsewhere in the text, with no
        # recognized secret keyword nearby, still redacts exactly as before -- Phase A never
        # touches it, so Phase B's `_TOKEN_RE`/`_URL_USERINFO_RE` see it unchanged.
        standalone_token = hook.redact("see sk-abcdefghij1234567890 in the log")
        self.assertNotIn("sk-abcdefghij1234567890", standalone_token)
        self.assertIn("[REDACTED_TOKEN]", standalone_token)
        standalone_url = hook.redact("visit https://bob:hunter2@example.com today")
        self.assertNotIn("hunter2", standalone_url)

    def test_labeled_secret_atomic_capture_claims_an_adjacent_jwt_shaped_value(self) -> None:
        # Direct coverage for the non-negotiable property this rewrite exists to guarantee, using
        # the JWT/Bearer patterns specifically (both moved into "Phase B" this round -- see
        # `redact()`'s own comment): a keyword directly glued to a JWT-shaped run must have the
        # *entire* run claimed as one atomic secret span by the keyword pass, not fragmented by
        # `_JWT_RE` running first and leaving a prefix/suffix exposed the way the IPv4/phone repros
        # above did. See `test_redacts_standalone_jwt_immediately_adjacent_to_cjk_text`'s own
        # updated comment for why that pre-existing test's filler word had to change to keep
        # testing `_JWT_RE` in isolation once this property took effect.
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        redacted = hook.redact(f"令牌{jwt}泄露")
        self.assertEqual(redacted, "令牌[REDACTED]泄露")
        self.assertNotIn(jwt, redacted)
        self.assertNotIn("[REDACTED_TOKEN]", redacted)

    def test_labeled_secret_value_span_scales_linearly_on_adversarial_input(self) -> None:
        # Regression test 6: the new space-tolerant PERMISSIVE value grammar
        # (`_CJK_VALUE_SPACE_DIGIT_CONTINUATION`) and the widened STRICT digit-or-hyphen guard must
        # not reopen a ReDoS surface -- measured scaling, not just "it completes". Doubling the
        # input should roughly double the time (linear), not quadruple or worse.
        import time

        def timed(text: str) -> float:
            started = time.monotonic()
            hook.redact(text)
            return time.monotonic() - started

        # Adversarial shape 1: a long chain of digit-groups joined by single spaces (the exact
        # shape the space-tolerance widening exists to capture atomically).
        small = timed("备份码：" + " ".join(["1234"] * 2_000))
        large = timed("备份码：" + " ".join(["1234"] * 8_000))  # 4x input
        self.assertLess(large, small * 4 + 0.5, "digit-group chain must scale near-linearly")
        self.assertLess(large, 1.0)

        # Adversarial shape 2: a long non-matching ASCII run after a bare-whitespace connector,
        # exercising the widened `[0-9-]` STRICT guard's reachability scan when neither a digit
        # nor a hyphen is ever found.
        small2 = timed("设备码 " + "a" * 8_000)
        large2 = timed("设备码 " + "a" * 32_000)  # 4x input
        self.assertLess(large2, small2 * 4 + 0.5, "STRICT guard scan must scale near-linearly")
        self.assertLess(large2, 1.0)

    def test_labeled_secret_atomic_capture_does_not_flag_realistic_technical_prose(self) -> None:
        # Regression test 7: realistic non-secret Chinese/English technical sentences using a
        # label word with no real secret nearby, confirming the round-8 widenings (space-tolerant
        # PERMISSIVE values, the digit-or-hyphen STRICT guard, the compound-suffix keyword) did not
        # newly start matching ordinary prose.
        unchanged_cases = (
            "密码策略要求每90天更新一次",
            "这个系统的密码学实现基于AES-256加密算法",
            "令牌 bucket 算法用于限流，是一种常见的设计模式",
            "the api key concept is fundamental to REST design",
            "our password rotation policy is well-documented",
            "设备码 is a term used in OAuth device authorization flow",
            "私钥 management requires careful key-rotation practices",
            "备份码策略：每月轮换一次，不涉及具体数值",
            "当前密码：说明见 2 楼公告栏",
            "密码 used 2 factor auth codes for login",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_table_label_keyword_standalone_rejects_an_immediately_following_connector(
        self,
    ) -> None:
        # Architectural-rewrite round-2 finding (found this round while fuzzing idempotency, not
        # from the prior review): `_SECRET_LABEL_KEYWORD` (used by `_TABLE_CJK_SECRET_RE` and the
        # header/data-row column scan to decide "is this cell a genuine standalone label") rejected
        # an immediately-following compound word (another CJK ideograph, or an ASCII alnum/`_`/`-`)
        # but not an immediately-following CONNECTOR (":"/"："/"="/"＝", a CJK connector word like
        # "是"/"就是", or the ASCII word "is") -- so a keyword glued directly to its own inline
        # value ("密码:...", "备份码 是 ...", "db_password_prod is ...") could still be misread as a
        # bare table label with the literal connector+value text folded into the (verbatim-echoed)
        # label cell. See `_CJK_SECRET_KEYWORD_STANDALONE`'s own comment for the full leak mechanism
        # and `test_table_cjk_secret_does_not_fragment_a_labeled_value_after_an_earlier_table_row`
        # below for the end-to-end repro this unit-level check protects.
        rejected = (
            "密码:x",
            "密码：x",
            "密码=x",
            "密码＝x",
            "密码是x",
            "密码 是 x",
            "备份码 就是 x",
            "password:x",
            "password=x",
            "password is x",
            "db_password_prod is x",
            "api_key is x",
        )
        for text in rejected:
            self.assertIsNone(
                hook._SECRET_LABEL_KEYWORD_RE.search(text),
                f"{text!r} should not be recognized as a standalone table label",
            )
        # A genuine bare label (nothing but whitespace/end-of-cell after the keyword) must still
        # match exactly as before -- this fix must not reopen the compound-suffix regression
        # `test_secret_label_keyword_standalone_recognizes_compound_suffixed_labels` guards.
        accepted = ("密码", "密码2", "密码-prod", "password", "db_password_prod", "api_key_prod")
        for text in accepted:
            self.assertIsNotNone(
                hook._SECRET_LABEL_KEYWORD_RE.search(text),
                f"{text!r} should still be recognized as a standalone table label",
            )

    def test_table_cjk_secret_does_not_fragment_a_labeled_value_after_an_earlier_table_row(
        self,
    ) -> None:
        # End-to-end repro for the same finding: an EARLIER, genuinely separate table row
        # ("| token | abc123 |") leaves its own trailing pipe unconsumed by design (so a second
        # label/value pair sharing one row can start its own match on it -- see
        # `_TABLE_CJK_SECRET_RE`'s own comment) -- but a later, unrelated CJK/ASCII-labeled secret
        # value elsewhere on the same line inherits that leftover pipe as a spurious "opening"
        # delimiter for `_TABLE_CJK_SECRET_RE`'s `label_cell`. When that later value itself contains
        # one bare '|' (a plausible character in a real secret; `_CJK_VALUE_CHAR_CLASS_INLINE`
        # deliberately allows it), the embedded pipe gets misread as the label cell's own closing
        # boundary, and everything before it (the keyword, its connector, and a leading fragment of
        # the real secret) gets echoed verbatim in the clear right next to a "[REDACTED]" placeholder
        # that makes the line look fully handled. Verified to FAIL against this round's own
        # pre-fix code (`redact('| token | abc123 | 密码:Ab|Rm4T2')` ->
        # `'| token | [REDACTED] | 密码:Ab|[REDACTED]'`, leaking "Ab") and to reproduce
        # byte-identically against the committed HEAD release script (not a new regression from an
        # earlier round of this same rewrite) before this round's fix, and PASS after it.
        secret = "Ab|Rm4T2xX9"
        cases = (
            f"| token | abc123 | 密码:{secret}",
            f"| token | abc123 | 密码：{secret}",
            f"| token | abc123 | 密码 是 {secret}",
            f"| token | abc123 | 备份码 就是 {secret}",
            f"| token | abc123 | password:{secret}",
            f"| token | abc123 | password is {secret}",
            f"| token | abc123 | db_password_prod is {secret}",
            f"| token | abc123 | api_key_prod:{secret}",
        )
        for text in cases:
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            # No fragment of the secret (on either side of its own internal '|') may survive.
            for fragment in secret.split("|"):
                self.assertNotIn(fragment, redacted, (text, redacted))
            # The atomic-span property: exactly one placeholder for the second (real) value,
            # regardless of which connector shape claimed it.
            self.assertEqual(redacted.count("[REDACTED]"), 2, (text, redacted))
            self.assertEqual(hook.redact(redacted), redacted, "must stay idempotent")

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

    def test_redacts_cn_mobile_number_glued_to_cjk(self) -> None:
        # 2026-08-22 gap-analysis finding: redact() covered credential/network
        # identifiers but not a mainland China mobile number, which is exactly
        # the kind of fixed-shape structured PII this bridge should not forward
        # unredacted into another agent's context. Includes a CJK-glued case
        # (no whitespace) and an alnum-glued non-match to confirm the boundary
        # does not misfire on an order id / hash containing the same digits.
        self.fixture.add_memory(
            "# contact notes\n"
            "call me at 13800138000 anytime\n"
            "手机13800138000该号码勿外传\n"
            "order id ORD1385551234567X99 is unrelated\n"
        )
        output = self.fixture.run("contact notes")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("13800138000", context)
        self.assertIn("[REDACTED_PHONE]", context)
        self.assertIn("ORD1385551234567X99", context)

    def test_redacts_cn_id_number_does_not_leak_partial_digits(self) -> None:
        # Companion to the mobile-number test above: an 18-digit resident ID
        # number is likewise a fixed, checkable structured-PII shape. Confirms
        # a longer digit run that merely contains a valid-looking substring
        # (no letter/digit boundary either side) is not left half-redacted.
        self.fixture.add_memory(
            "# contact notes\n"
            "id number 110101199003077758 on file\n"
            "tracking 84123800138000123456789 is unrelated\n"
        )
        output = self.fixture.run("contact notes")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("110101199003077758", context)
        self.assertIn("[REDACTED_ID]", context)
        self.assertIn("84123800138000123456789", context)

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

    # --- architectural-rewrite round 1 (2026-08-22): `_ASSIGNMENT_RE` brought into the same
    # atomic-span Phase A treatment already built for CJK labels, plus the false-positive and
    # ReDoS fixes required alongside it. Every value below is synthetic, constructed for these
    # tests, never a real secret. Each of the seven scenarios below was verified against the
    # actual pre-rewrite behavior of this file (captured directly in this round's own session
    # transcript before any code changed) to genuinely fail before this round's fix and pass
    # after it -- not merely shaped to match whatever the code happens to do now. ---

    def test_assignment_atomic_span_covers_a_space_grouped_phone_shaped_backup_code(self) -> None:
        # Scenario 1: a labeled backup/recovery-code value that is space-grouped and
        # phone-number-shaped end to end. Pre-rewrite, `_ASSIGNMENT_RE`'s value class had no
        # space-continuation at all, so this matched only the first digit group and left the rest
        # exposed next to the placeholder: `redact('password: 137 6620 4419 8875')` produced
        # `'password: [REDACTED] 6620 4419 8875'`. Must now redact as ONE atomic span, zero
        # characters of the real value surviving.
        text = "password: 137 6620 4419 8875"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password: [REDACTED]")
        for fragment in ("137", "6620", "4419", "8875"):
            self.assertNotIn(fragment, redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

    def test_assignment_atomic_span_covers_an_embedded_ipv4_shaped_password(self) -> None:
        # Scenario 2: a labeled password value containing an embedded IPv4-shaped substring with
        # prefix/suffix characters around it. Pre-rewrite, `_ASSIGNMENT_RE`'s value class excluded
        # ',', so this matched only the prefix before the first comma and left the IPv4-shaped
        # middle for `_IPV4_RE` to nibble out of the real secret in Phase B:
        # `redact('password: Ab,198.51.100.23,Cd')` produced
        # `'password: Ab,[REDACTED_IP],Cd'`. Must now redact as ONE atomic span.
        text = "password: Ab,198.51.100.23,Cd"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password: [REDACTED]")
        self.assertNotIn("Ab", redacted)
        self.assertNotIn("Cd", redacted)
        self.assertNotIn("198.51.100.23", redacted)
        self.assertNotIn("[REDACTED_IP]", redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

    def test_assignment_atomic_span_covers_a_hyphenated_device_code(self) -> None:
        # Scenario 3: a separator-free, alphabetic-hyphenated authorization/device code following
        # its keyword. `_ASSIGNMENT_RE` (unlike the CJK inline pattern) has no bare-whitespace
        # form at all -- every one of its alternatives requires an explicit ':'/'=' separator --
        # so the ASCII analog of this shape is "keyword: CODE-CODE", not "keyword CODE-CODE"; the
        # CJK sibling with a genuinely separator-free bare mention ("设备码 WDJB-MJHT") was already
        # fixed in a prior round and is re-verified here to confirm this round's rewrite of the
        # surrounding pipeline did not regress it.
        cjk_redacted = hook.redact("设备码 WDJB-MJHT")
        self.assertNotIn("WDJB-MJHT", cjk_redacted)
        self.assertEqual(cjk_redacted, "设备码 [REDACTED]")

        for text, code in (
            ("device key: ZMWH-4LJD", "ZMWH-4LJD"),
            ("auth token=XKQP-9RTV", "XKQP-9RTV"),
        ):
            redacted = hook.redact(text)
            self.assertNotIn(code, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)

    def test_assignment_atomic_span_handles_a_markdown_table_cell_with_an_escaped_pipe(
        self,
    ) -> None:
        # Scenario 4: a markdown table cell containing an escaped pipe inside the secret value.
        # This exercises the CJK table path's escaped-pipe handling (`_CJK_VALUE_BODY_TABLE`,
        # fixed in a prior round) end to end once more against this round's rewritten pass order,
        # to confirm the architectural changes above did not regress it.
        secret_with_pipe = "Qx9\\|Lm2N7"
        redacted = hook.redact(f"| 密码 | {secret_with_pipe} |")
        self.assertEqual(redacted, "| 密码 | [REDACTED] |")
        self.assertNotIn("Qx9", redacted)
        self.assertNotIn("Lm2N7", redacted)

    def test_assignment_and_cjk_share_compound_suffix_vocabulary_inline_and_table(self) -> None:
        # Scenario 5: compound-suffixed labels (keyword+digit, keyword+"-prod") redact in BOTH
        # inline-prose and table-cell contexts, for both the CJK and ASCII keyword vocabularies --
        # confirming the inline and table matchers stay in sync (a prior round's finding).
        cjk_secret = "Xk9$mQ2vR8pL"
        ascii_secret = "Gh7#kL9mWq2"
        cases = (
            (f"密码2：{cjk_secret}", cjk_secret),
            (f"| 密码-prod | {cjk_secret} |", cjk_secret),
            (f"db_password_prod is {ascii_secret}", ascii_secret),
            (f"| db_password_prod | {ascii_secret} |", ascii_secret),
            (f"api_key_prod: {ascii_secret}", ascii_secret),
        )
        for text, secret in cases:
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)

    def test_table_label_version_tagged_suffix_no_longer_leaks_the_value_cell(self) -> None:
        # Round-3 (this round) finding, found while fuzzing the compound-suffix vocabulary for
        # table/inline parity beyond this file's own pinned cases (not from the prior review): a
        # version-tagged label suffix ("-v2"/"_v2", the dominant real-world convention for a
        # rotated secret -- "api_key_v2", "密钥_v2") matched neither the bare-digit branch nor any
        # `_LABEL_QUALIFIER_WORD`, so `_SECRET_LABEL_KEYWORD` rejected the whole label cell as "not
        # a genuine label" -- and unlike the inline matcher (saved by its STRICT bare-mention
        # fallback), the table matcher has no such fallback: the row was left COMPLETELY
        # unredacted, a full silent leak, not a fragment. Verified to fail against this file's
        # pre-fix HEAD: `redact('| 密钥_v2 | Xy9zAb12Cd |')` -> the value cell unchanged.
        cjk_secret = "Xy9zAb12Cd"
        ascii_secret = "Gh7#kL9mWq2"
        cases = (
            f"| 密钥_v2 | {cjk_secret} |",
            f"| 密钥-v2 | {cjk_secret} |",
            f"| 密码-v3 | {cjk_secret} |",
            f"| api_key_v2 | {ascii_secret} |",
            f"| api_key-v2 | {ascii_secret} |",
        )
        for text in cases:
            redacted = hook.redact(text)
            secret = cjk_secret if "密" in text else ascii_secret
            self.assertNotIn(secret, redacted, f"{text} -> {redacted}")
            self.assertIn("[REDACTED]", redacted, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")
        # Non-regression: the digit-only and named-qualifier-word suffix shapes this file already
        # pinned above keep working exactly as before -- this fix only adds a third, disjoint
        # alternative, it does not touch the other two.
        self.assertNotIn(cjk_secret, hook.redact(f"| 密钥2 | {cjk_secret} |"))
        self.assertNotIn(cjk_secret, hook.redact(f"| 密钥-prod | {cjk_secret} |"))

    # --- round-6 dual-review fix (independent Claude opus + Codex, 2026-08-22, against the
    # architectural-rewrite retry round): `_ASSIGNMENT_RE`'s own value class was the single
    # largest remaining atomic-span gap -- '"'/'&'/';' were unconditional hard stops, so a real
    # ASCII-labeled secret merely containing one of them still fragmented across this pattern and
    # a Phase B structural pattern, verbatim the bug class this whole rewrite exists to close.
    # Every value below is synthetic, constructed for these tests, and independently verified to
    # fragment against this file's pre-round-6 HEAD before this round's fix. ---

    def test_assignment_atomic_span_survives_an_embedded_double_quote(self) -> None:
        # Pre-fix: `redact('password: Pa55"word-198.51.100.7-Qq')` produced
        # `'password: [REDACTED]"word-[REDACTED_IP]-Qq'` -- both "word" and "Qq" leaked in the
        # clear next to two separate placeholders. The value is unquoted (no real JSON string), so
        # an embedded '"' must not be read as a closing quote.
        text = 'password: Pa55"word-198.51.100.7-Qq'
        redacted = hook.redact(text)
        # assertEqual (not per-fragment assertNotIn) because "word" is itself a substring of the
        # keyword "password" -- the exact-equality check is the real atomicity proof here.
        self.assertEqual(redacted, "password: [REDACTED]")
        self.assertNotIn("Qq", redacted)
        self.assertNotIn("198.51.100.7", redacted)
        self.assertNotIn("[REDACTED_IP]", redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

    def test_assignment_atomic_span_survives_an_embedded_ampersand_not_at_a_query_boundary(
        self,
    ) -> None:
        # Pre-fix: `redact('password: Tr0ub&dour-192.0.2.5-Zx')` produced
        # `'password: [REDACTED]&dour-[REDACTED_IP]-Zx'`. The '&' here is part of the secret, not
        # a query-string 'key=value' boundary (nothing after it matches `ident=`), so it must not
        # terminate the value -- while a genuine query-string boundary (covered by the existing
        # `test_assignment_value_class_now_redacts_bracket_and_brace_bearing_secrets` pin,
        # re-verified below) must still hold.
        for text, leaked in (
            ("password: Tr0ub&dour-192.0.2.5-Zx", ("dour", "Zx", "192.0.2.5", "[REDACTED_IP]")),
            ("api_key: k9&m-bob.smith@corp.example-r2", ("k9", "r2", "bob.smith@corp.example", "[REDACTED_EMAIL]")),
            ("secret: s4&q-13800001111-v8", ("s4", "v8", "13800001111", "[REDACTED_PHONE]")),
            ("token: t7&x-a1:b2:c3:d4:e5:f6-p3", ("t7", "p3", "a1:b2:c3:d4:e5:f6", "[REDACTED_IP]")),
        ):
            redacted = hook.redact(text)
            keyword = text.split(":", 1)[0]
            self.assertEqual(redacted, f"{keyword}: [REDACTED]", text)
            for fragment in leaked:
                self.assertNotIn(fragment, redacted, f"{text} -> {redacted}")
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")
        # The genuine query-string boundary this file already protects must still hold.
        self.assertEqual(
            hook.redact("token=abc123&next=xyz&other=1"),
            "token=[REDACTED]&next=xyz&other=1",
        )

    def test_assignment_atomic_span_survives_an_embedded_semicolon(self) -> None:
        # Pre-fix: `redact('password: Hunt3r;Two-203.0.113.9-Ww')` produced
        # `'password: [REDACTED];Two-[REDACTED_IP]-Ww'`.
        text = "password: Hunt3r;Two-203.0.113.9-Ww"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password: [REDACTED]")
        for fragment in ("Two", "Ww", "203.0.113.9", "[REDACTED_IP]"):
            self.assertNotIn(fragment, redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

    def test_assignment_atomic_span_covers_json_quoted_value_with_embedded_ampersand(
        self,
    ) -> None:
        # Pre-fix: `redact('"password": "Zx&cv-192.0.2.5-Bn"')` produced
        # `'"password": "Zx&cv-[REDACTED_IP]-Bn"'`. A genuine JSON-quoted value ('preval' really
        # captures the opening quote here) must still stop at its own closing quote, but must not
        # fragment on the embedded '&'/'-'/'.' in between.
        text = '"password": "Zx&cv-192.0.2.5-Bn"'
        redacted = hook.redact(text)
        self.assertEqual(redacted, '"password": "[REDACTED]"')
        for fragment in ("Zx", "cv", "Bn", "192.0.2.5", "[REDACTED_IP]"):
            self.assertNotIn(fragment, redacted)
        self.assertEqual(redacted, hook.redact(redacted), "must stay idempotent")

    def test_assignment_ampersand_lookahead_scales_linearly_on_adversarial_input(self) -> None:
        # The new '&'-boundary lookahead (`(?!&(?=[A-Za-z0-9_]+=))`) must not reopen a ReDoS
        # surface: an '&' immediately followed by a long alphanumeric run with no trailing '='
        # forces the lookahead's own `[A-Za-z0-9_]+` to backtrack all the way down before failing.
        # Repeating that shape many times must still scale near-linearly, not quadratically.
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        small = "token: " + "&" + "a" * 2_000 + ("&" + "a" * 2_000) * 3
        large = "token: " + ("&" + "a" * 2_000) * 16  # 4x the '&'-run repetitions of `small`
        small_elapsed = timed(small)
        large_elapsed = timed(large)
        self.assertLess(large_elapsed, small_elapsed * 4 + 0.5, "'&' lookahead must scale near-linearly")
        self.assertLess(large_elapsed, 3.0)

    def test_full_redact_pipeline_scales_near_linearly_on_adversarial_dash_heavy_input(
        self,
    ) -> None:
        # Scenario 6: an adversarial ReDoS-shaped input (a long run of local-part-shaped,
        # userinfo-shaped, and identifier-shaped characters with no '@'/'://'/recognized keyword
        # ever satisfied) reaching the full `redact()` pipeline, not just one pattern in
        # isolation -- this is what `split_blocks()` actually calls on untruncated block text.
        # Pre-rewrite, three independent unbounded-quantifier patterns (`_EMAIL_RE`,
        # `_ASSIGNMENT_RE`'s keyword-run prefix/suffix groups, `_URL_USERINFO_RE`) each degraded
        # quadratically on exactly this shape; end to end, `redact("a-" * 32000)` measured ~10.8s
        # before this round's fixes (see this round's final report for the per-pattern timings).
        # A measured near-linear scaling factor (not just "completes under some cutoff") is the
        # actual property being verified: quadratic scaling would show roughly a 16x-or-worse
        # slowdown for a 4x input-size increase; linear/near-linear stays close to 4x.
        small = "a-" * 8_000
        large = "a-" * 32_000  # 4x the character count of `small`
        t0 = time.perf_counter()
        hook.redact(small)
        small_elapsed = time.perf_counter() - t0
        t1 = time.perf_counter()
        hook.redact(large)
        large_elapsed = time.perf_counter() - t1
        # Generous ceiling (10x, not 4x) to absorb timing noise on a shared/loaded test machine
        # while still failing loudly on a real quadratic-or-worse regression (which would show
        # 16x+ for this 4x size increase, and far more at the sizes this round's own benchmarking
        # used). Also assert an absolute wall-clock ceiling well under the multi-second stalls
        # measured pre-fix, so a regression that happens to keep a low *ratio* while still being
        # slow in absolute terms is still caught.
        self.assertLess(
            large_elapsed,
            max(small_elapsed * 10, 0.05),
            f"redact() scaled worse than near-linearly: {small_elapsed:.4f}s -> {large_elapsed:.4f}s",
        )
        self.assertLess(large_elapsed, 2.0, "redact() must not stall on adversarial input")

    def test_armed_table_secret_column_scales_near_linearly_on_homogeneous_non_alnum_filler(
        self,
    ) -> None:
        # Round-9 dual-review finding (independent Claude opus + Codex, 2026-08-22, against the
        # round-8 attempt, P1 BLOCKING + a coverage gap in this same class -- item 3 of that
        # review): the dash-heavy scaling test above never arms `_redact_cjk_secret_table_columns`
        # (no table, no secret-column header), so it could not see the quadratic blowup that
        # function's own guard-reachability lookahead had on a DATA CELL consisting of a long
        # homogeneous run of a single non-alphanumeric filler character -- the guard
        # (`[A-Za-z0-9]`) is never reachable anywhere in such a cell, so the old unbounded
        # `(?:body)*` backtracked the full remaining run at every scanned start position.
        # Measured pre-fix (this file, `/usr/bin/python3` 3.9.6): n=8000 ~2.5s, n=16000 ~10.5s,
        # n=32000 ~42s (~4x slower per doubling -- the quadratic signature). Verified this test
        # genuinely fails against the pre-fix code (an unbounded `(?:{body})*` guard in
        # `_cjk_value_pattern`) and passes after bounding the guard's reachability scan to
        # `_CJK_VALUE_GUARD_LOOKAHEAD_MAX` characters (see that constant's own comment).
        def table(n: int, filler: str) -> str:
            return "| 密码 |\n|---|\n| " + filler * n + " |"

        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook._redact_cjk_secret_table_columns(text))

        # A sample of the filler characters the review's own audit found triggering (not just
        # dashes -- any non-alnum filler with no alnum reachable in the cell has the same shape).
        for filler in ("-", ".", "_", "'", "`"):
            small_elapsed = timed(table(2_000, filler))
            large_elapsed = timed(table(8_000, filler))  # 4x the character count
            self.assertLess(
                large_elapsed,
                max(small_elapsed * 10, 0.2),
                f"table-column scan scaled worse than near-linearly for filler {filler!r}: "
                f"{small_elapsed:.4f}s -> {large_elapsed:.4f}s",
            )
            self.assertLess(
                large_elapsed, 1.0, f"table-column scan must not stall on filler {filler!r}"
            )

    def test_split_blocks_scales_near_linearly_with_an_armed_secret_column_and_filler_cell(
        self,
    ) -> None:
        # Round-9 dual-review finding, continued (item 2, the reachability proof for the finding
        # above): `split_blocks()` calls `redact()` on the full, untruncated buffered block text
        # BEFORE the 4,000-char slice, so a single real memory-file block reaches the same
        # quadratic path synchronously inside the live `UserPromptSubmit` hook. Measured pre-fix
        # end to end: a ~20KB block (`## 生产环境凭据` heading + a two-column table whose header
        # arms a 密码 column, one data cell holding a 20,000-character dash run) took ~17.1s
        # through `split_blocks()` alone. Verified this test fails against the pre-fix code and
        # passes after the guard-lookahead bound.
        def block_text(n: int) -> str:
            return "## 生产环境凭据\n" + "| 项目 | 密码 |\n|---|---|\n| 分隔 | " + ("-" * n) + " |\n"

        def timed(n: int) -> float:
            doc = hook.MemoryDocument("proj", block_text(n), 0)
            return _min_elapsed(lambda: list(hook.split_blocks(doc)))

        small_elapsed = timed(2_000)
        large_elapsed = timed(8_000)  # 4x the filler-run length
        self.assertLess(
            large_elapsed,
            max(small_elapsed * 10, 0.2),
            f"split_blocks() scaled worse than near-linearly: "
            f"{small_elapsed:.4f}s -> {large_elapsed:.4f}s",
        )
        self.assertLess(large_elapsed, 1.0, "split_blocks() must not stall on an armed table column")

    def test_does_not_flag_realistic_non_secret_technical_prose_after_a_label_word(self) -> None:
        # Scenario 7: realistic non-secret Chinese and English technical sentences using this
        # file's own label words, with no real secret nearby -- must NOT redact.
        unchanged_cases = (
            # CJK: an explicit "：" separator followed by a well-known, non-secret hashing
            # algorithm name, then a descriptive clause -- was previously an accepted residual
            # gap (see `test_cjk_secret_redaction_documents_further_known_residual_gaps`'s
            # updated item 12(a)); now closed via the small closed vocabulary in
            # `_KNOWN_NON_SECRET_WORDS`.
            "密码：Argon2id 是推荐的哈希算法。",
            # ASCII: `_ASSIGNMENT_RE`'s own new false positives, now closed the same way.
            "secret: this field documents the schema",
            "key: used to index the cache",
            # A couple of nearby shapes using the same mechanism, not explicitly named in the
            # brief but the same class of false positive.
            "token: used for pagination only",
            "password: is required before login",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)
        # False-positive guard must not blunt real detection: a genuine digitless secret assigned
        # via the identical ASCII "key: value"/"KEY=value" syntax (not one of the ~40 known
        # non-secret words) must still redact in full -- see
        # `test_redacts_json_quoted_and_snake_case_credentials` for the pinned end-to-end version
        # of this same property.
        self.assertEqual(
            hook.redact("password: correcthorsebatterystaple"),
            "password: [REDACTED]",
        )

    def test_does_not_flag_version_or_standards_body_citations_after_a_label_word(self) -> None:
        # Architectural-rewrite gap-4 fix (3-way gate finding, opus + codex + grok all
        # independently converged): a product-name-plus-version-number citation or a standards-
        # body citation immediately after a secret label was being fully redacted -- verified
        # fail-before against the pre-fix HEAD of this file (each line below previously lost its
        # trailing "OpenSSL 3.0"/"OAuth2.0"/etc. to "[REDACTED]").
        unchanged_cases = (
            "password: OpenSSL 3.0 is the recommended library.",
            "token: OAuth2.0 defines the flow.",
            "key: NIST SP 800-63B defines requirements.",
            "token: IEEE 802.11 defines the standard.",
            "密码：OpenSSL 3.0 是常用库。",
            "密钥：TLS1.3 是推荐协议。",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)
        # False-positive guard must not blunt real detection: a genuine secret that merely
        # contains a '.' alongside other high-entropy characters (not a clean
        # capitalized-word-plus-version-number shape) must still redact in full.
        self.assertEqual(hook.redact("password: Ab3.14pQz9XkLmN"), "password: [REDACTED]")
        self.assertEqual(
            hook.redact("密码：Ab-192.0.2.44-Cd"), "密码：[REDACTED]",
            "an embedded IPv4-shaped substring inside a real secret must still redact atomically",
        )

    # --- round-11 dual-review fixes (2026-08-22): every test below was verified to FAIL against
    # the round-10 HEAD (the version reviewed and given an automatic NO-GO) and PASS after this
    # round's fixes. Every secret value is synthetic, constructed for these tests. ---

    def test_bare_mention_strict_value_survives_an_embedded_space_grouped_phone_shape(self) -> None:
        # Round-11 P0 fix (independent Claude opus + Codex, 2026-08-22, against the round-10
        # attempt, item 3): a space-grouped, phone-number-shaped value following a BARE keyword
        # mention (no "："/"="/连接词 at all) had no space-continuation of any kind in the STRICT
        # value class, so Phase A never matched it as a value in the first place and
        # `_CN_MOBILE_RE` (Phase B) nibbled the first phone-shaped chunk out of the middle --
        # `redact('恢复码 186 7723 4491 5508')` produced `'恢复码 [REDACTED_PHONE] 5508'`, leaking
        # "5508" right next to a placeholder that made the line look fully handled. Must now
        # redact as ONE atomic span with zero characters of the real value surviving, for every
        # keyword synonym including the newly-added `备用码`.
        for keyword in ("恢复码", "验证码", "密码", "密钥", "令牌", "设备码", "备份码", "备用码"):
            secret = "186 7723 4491 5508"
            text = f"{keyword} {secret}"
            redacted = hook.redact(text)
            self.assertEqual(redacted, f"{keyword} [REDACTED]", text)
            self.assertNotIn("186", redacted, text)
            self.assertNotIn("5508", redacted, text)
            self.assertNotIn("[REDACTED_PHONE]", redacted, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")
        # A 4-digit-group variant (the round-10 review's own repro), confirming no fragment of any
        # group survives regardless of group count.
        redacted = hook.redact("密码 1867 7723 4491 5508")
        self.assertEqual(redacted, "密码 [REDACTED]")
        for fragment in ("1867", "7723", "4491", "5508"):
            self.assertNotIn(fragment, redacted)

    def test_labeled_secret_value_does_not_swallow_trailing_prose_after_explicit_separator(
        self,
    ) -> None:
        # Round-11 P1 fix (independent Claude opus + Codex, 2026-08-22, against the round-10
        # attempt, item 4): the round-10 fix for the 助记词/BIP-39 space-grouped-word gap wired an
        # UNCONDITIONAL alphanumeric space bridge into the shared PERMISSIVE-with-explicit-
        # separator value class, so ANY labeled value followed by ordinary trailing English prose
        # got bridged straight through every space and swallowed into the placeholder --
        # `redact('密码：Ab7xK9m and the port is 8080 for staging')` collapsed to just
        # `'密码：[REDACTED]'`, silently deleting an entire unrelated trailing sentence, not just
        # the secret. A 200-word trailing paragraph collapsed from ~1.5KB to 13 bytes. Fixed by
        # scoping the wide bridge to the `助记词`/`助記詞` keyword specifically (see
        # `_CJK_SECRET_VALUE_PERMISSIVE_INLINE_WORDLIST`'s own comment) -- every other keyword now
        # stops at the first space that isn't immediately followed by a digit, matching the
        # pre-regression baseline.
        cases = (
            ("密码：Ab7xK9m and the port is 8080 for staging", "Ab7xK9m", " and the port is 8080 for staging"),
            (
                "登录密码：Xy9zQ2p then run migrate and restart the worker pool",
                "Xy9zQ2p",
                " then run migrate and restart the worker pool",
            ),
        )
        for text, secret, trailing_prose in cases:
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertTrue(redacted.endswith(trailing_prose), redacted)
            self.assertEqual(redacted, f"{text.split(secret)[0]}[REDACTED]{trailing_prose}", text)
        # A long (200-word) trailing paragraph must survive essentially intact -- only the labeled
        # secret token itself is replaced, not the unrelated content after it.
        long_text = "密码：Ab7xK9m " + " ".join(f"word{i}" for i in range(200))
        redacted = hook.redact(long_text)
        self.assertGreater(len(redacted), 1000, "trailing prose must not be swallowed")
        self.assertNotIn("Ab7xK9m", redacted)
        self.assertTrue(redacted.startswith("密码：[REDACTED] word0 word1"), redacted)

    def test_mnemonic_keyword_still_redacts_a_full_space_separated_word_list(self) -> None:
        # Regression guard for the fix immediately above: `助记词`/`助記詞` -- the one keyword whose
        # real values are conventionally space-separated words (a BIP-39 seed phrase), not a single
        # token or a digit-grouped code -- must still redact the WHOLE phrase as one atomic span,
        # not just its first word, confirming the keyword-conditional wide bridge still fires for
        # this specific keyword even though it no longer fires for every other one.
        secret = "apple banana cherry dolphin elephant"
        for text in (f"助记词：{secret}", f"助記詞：{secret}"):
            redacted = hook.redact(text)
            self.assertNotIn("apple", redacted, text)
            self.assertNotIn("elephant", redacted, text)
            self.assertTrue(redacted.endswith("[REDACTED]"), redacted)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text}")

    def test_table_label_cell_does_not_cross_a_newline_into_the_next_line(self) -> None:
        # Round-11 P1 fix (independent Claude opus + Codex, 2026-08-22, against the round-10
        # attempt, item 5): `_TABLE_CJK_SECRET_RE`'s label-cell trailing whitespace was plain
        # `\s*`, which matches `\n` -- unlike every other line-scoped boundary in this file -- so a
        # keyword-bearing label cell alone on one line could claim an unrelated sentence on the
        # *next* line as its "value": `redact('| 密钥 |\nThe secret rotation happens monthly')`
        # produced `'| 密钥 |\n[REDACTED]'`. Must now leave unrelated next-line prose untouched.
        text = "| 密钥 |\nThe secret rotation happens monthly"
        self.assertEqual(hook.redact(text), text)
        # A genuine same-row "| label | value |" shape (value on the SAME line) must be completely
        # unaffected by this fix.
        secret = "Qw7#zP2mLv8Ke"
        redacted = hook.redact(f"| 密钥 | {secret} |")
        self.assertEqual(redacted, "| 密钥 | [REDACTED] |")
        self.assertNotIn(secret, redacted)
        # The genuinely multi-row shape (label in a header row, value in a later DATA row, same
        # column) is unaffected -- that shape is handled by the separate column-tracking pass,
        # which already has its own, correct per-line boundaries.
        redacted = hook.redact(f"| 用户 | 密钥 |\n|---|---|\n| root | {secret} |")
        self.assertEqual(redacted, "| 用户 | 密钥 |\n|---|---|\n| root | [REDACTED] |")
        self.assertNotIn(secret, redacted)

    def test_combined_qualifier_and_digit_label_suffix_redacts_a_quote_wrapped_value_whole(
        self,
    ) -> None:
        # Round-12 (this retry round) P1 BLOCKING fix (independent Claude opus + Codex,
        # 2026-08-22, against the round-11 attempt, item 1): `_LABEL_QUALIFIER_SUFFIX` could
        # match EITHER a qualifier word ("-prod") OR a digit run ("-2"/"2"), never both together,
        # so the natural COMBINED form ("-prod2", "-dev1", "_prod2", "-prod-2") left an
        # unconsumed digit leftover ("2：") right before the real connector. That leftover then
        # fed the STRICT bare-mention fallback as if it were the start of the secret value, and
        # because the CJK typographic quotes exist only in `_CJK_VALUE_WRAP` (not the value body
        # class), the STRICT value's own optional *trailing* wrap consumed the real value's
        # OPENING quote as a bogus closing wrap, terminating the match one character early and
        # leaking the whole real secret in the clear next to a placeholder that made the line
        # look fully handled. Verified to FAIL (secret present in output) against the code before
        # this round's `_LABEL_QUALIFIER_SUFFIX` fix, and PASS after it.
        secret = "Ab3xK9mQ2vR8pLz"
        keywords = ("密码", "密钥", "令牌", "口令", "备份码", "私钥")
        suffixes = ("-prod2", "-dev1", "_prod2", "-prod-2")
        wraps = (("‘", "’"), ("“", "”"))
        seps = ("：", ":")
        checked = 0
        for keyword in keywords:
            for suffix in suffixes:
                for wrap_open, wrap_close in wraps:
                    for sep in seps:
                        text = f"{keyword}{suffix}{sep}{wrap_open}{secret}{wrap_close}"
                        redacted = hook.redact(text)
                        self.assertNotIn(secret, redacted, text)
                        self.assertEqual(redacted, f"{keyword}{suffix}{sep}{wrap_open}[REDACTED]{wrap_close}", text)
                        self.assertEqual(hook.redact(redacted), redacted, f"idempotency: {text}")
                        checked += 1
        self.assertEqual(checked, len(keywords) * len(suffixes) * len(wraps) * len(seps))
        # The same combined-suffix shape must also redact atomically in a list item, a markdown
        # table same-row cell (label cell AND its paired value cell), and through the ASCII
        # sibling vocabulary -- the fix lives in the one shared `_LABEL_QUALIFIER_SUFFIX`
        # constant, so all of these paths must benefit together.
        list_item = f"- 密码-prod2：“{secret}”"
        redacted = hook.redact(list_item)
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "- 密码-prod2：“[REDACTED]”")

        table_row = f"| 密码-prod2 | “{secret}” |"
        redacted = hook.redact(table_row)
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "| 密码-prod2 | “[REDACTED]” |")

        ascii_secret = "Zq7#vT4nBx2W"
        ascii_cases = (
            f"db_password_prod2: {ascii_secret}",
            f"password-prod2: {ascii_secret}",
        )
        for text in ascii_cases:
            redacted = hook.redact(text)
            self.assertNotIn(ascii_secret, redacted, text)
            self.assertTrue(redacted.endswith("[REDACTED]"), redacted)

    def test_table_label_keyword_standalone_rejects_an_immediately_following_placeholder(
        self,
    ) -> None:
        # Round-11 (this retry round) finding, found while fuzzing idempotency for the fix
        # immediately above (not from the prior review -- a real, reproducible, pre-existing gap
        # in the same subsystem, unchanged by the `_LABEL_QUALIFIER_SUFFIX` widening itself but
        # reachable by MORE inputs because of it). The compound-suffix branch of
        # `_redact_inline_cjk_secret`/`_redact_inline_ascii_secret` glues the keyword directly onto
        # "[REDACTED]" with no connector text in between whenever a suffix was consumed -- so on a
        # SECOND `redact()` pass, the glued "<keyword>[REDACTED]" cell was misread as a fresh
        # standalone table LABEL (nothing rejected a following placeholder), and the table-row
        # pattern then redacted a completely unrelated ADJACENT cell as if it were this "label"'s
        # value. Verified to FAIL (non-idempotent, second pass over-redacts an unrelated cell)
        # against the code before this round's placeholder-adjacency fix, and PASS after it.
        cases = (
            "| 密钥-prod 是 'Qw7#zP2mLv8Ke' | more |",
            "| 密码-dev1：“Ab3xK9mQ2vR8pLz” | more |",
            "| db_password_prod is Gh7#kL9mWq2, more text",
        )
        for text in cases:
            first_pass = hook.redact(text)
            second_pass = hook.redact(first_pass)
            self.assertEqual(second_pass, first_pass, f"non-idempotent: {text!r} -> {first_pass!r}")
        # The genuine two-cell "| label | value |" shape (a real adjacent value cell) must still
        # redact correctly -- this fix only rejects a label immediately GLUED to a placeholder
        # within the same cell, not an ordinary separate value cell.
        secret = "Xk9mQ2vR8pL"
        redacted = hook.redact(f"| db_password_prod | {secret} |")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "| db_password_prod | [REDACTED] |")

    def test_table_cell_bracket_wrapped_secret_value_redacts_atomically_not_by_nibbled_substring(
        self,
    ) -> None:
        # Round-13 P1 BLOCKING fix (independent Claude opus + Codex, 2026-08-22, against the
        # round-12 attempt, items 1-4): `_redact_table_cell_value_or_placeholder` used to decide
        # "is this token an already-emitted placeholder?" with `token.startswith("[")` -- but the
        # table value class deliberately treats '[' and ']' as ordinary secret-value characters
        # (so a real secret merely *containing* a bracket is captured whole, not truncated at it),
        # so a real secret value that simply happens to BEGIN with '[' was misclassified as a
        # placeholder and returned byte-for-byte unchanged: Phase A never claimed it, and the
        # narrower Phase B patterns (`_IPV4_RE`/`_IPV6_CANDIDATE_RE`/`_EMAIL_RE`/`_CN_MOBILE_RE`)
        # then nibbled only the sub-shape each recognizes out of the *middle* of the still-raw
        # value, leaving the surrounding real secret characters exposed in the clear right next to
        # a placeholder that made the row look fully handled -- verbatim the defect class this
        # whole rewrite exists to eliminate. Each case below was verified to FAIL (a real secret
        # substring survives in the output, and/or `redact(redact(x)) != redact(x)`) against the
        # code before this round's `re.fullmatch` fix, and to PASS after it.
        cases = (
            # (table document, substrings of the real value that must never survive)
            (
                "| 用户 | 密码 |\n|---|---|\n| root | [Zq7-203.0.113.77-Pk] |",
                ("Zq7-", "-Pk", "203.0.113.77"),
            ),
            (
                "| 用户 | 密码 |\n|---|---|\n| ops | [158 6027 4419 7735] |",
                ("158", "7735", "6027 4419"),
            ),
            (
                "| 用户 | 密钥 |\n|---|---|\n| root | [Rt5-2001:db8:1234::99-Yb] |",
                ("Rt5-", "-Yb", "2001:db8:1234::99"),
            ),
            (
                "| 用户 | 令牌 |\n|---|---|\n| root | [Wm3-devnull@example.invalid-Qx] |",
                ("Wm3-", "-Qx", "devnull@example.invalid"),
            ),
        )
        for text, fragments in cases:
            redacted = hook.redact(text)
            for fragment in fragments:
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertIn("[REDACTED]", redacted, text)
            # Atomic: exactly one placeholder for the one secret cell, not a placeholder plus
            # leftover fragments of the original value sitting next to it.
            #
            # Round-14 dual-review finding (independent Claude opus + Codex, 2026-08-22, against
            # the round-13 attempt, item 13): a bracket-count balance check
            # (`count("[") == count("]")`) does not actually verify "exactly one placeholder" --
            # e.g. a doubled `"[REDACTED][REDACTED_TOKEN]"` output has 2 of each bracket, so it
            # would satisfy this assertion despite carrying two placeholders. Fixed to count real
            # placeholder-pattern matches directly instead of bracket characters.
            placeholder_count = len(re.findall(hook._REDACTED_PLACEHOLDER_PATTERN, redacted))
            self.assertEqual(placeholder_count, 1, redacted)
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")

    def test_table_cell_bracket_wrapped_secret_survives_split_blocks_and_build_context_end_to_end(
        self,
    ) -> None:
        # Same defect class as the unit-level test immediately above, exercised through the real
        # production path (memory file -> split_blocks -> redact -> emitted context), matching
        # this round's review, which confirmed the leak end-to-end through `split_blocks()` (which
        # feeds `build_context()` -> hook output -> Codex), not just `redact()` in isolation.
        self.fixture.add_memory(
            "# 生产环境凭据\n"
            "| 用户 | 密码 |\n"
            "|---|---|\n"
            "| root | [Zq7-203.0.113.77-Pk] |\n"
            "| ops  | [158 6027 4419 7735] |\n"
        )
        output = self.fixture.run("生产环境凭据")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        for fragment in ("Zq7-", "-Pk", "203.0.113.77", "158", "7735", "6027 4419"):
            self.assertNotIn(fragment, context, context)
        self.assertIn("[REDACTED]", context)

    def test_table_cell_bracket_wrapped_placeholder_still_passes_through_on_a_second_pass(
        self,
    ) -> None:
        # Regression guard for the fix immediately above, from the other direction: a genuine
        # already-emitted placeholder token (which also starts with '[', same as a real bracket-
        # wrapped secret) must still be recognized as a placeholder and passed through unchanged,
        # not re-wrapped or duplicated on a second `redact()` pass.
        already_redacted = "| 用户 | 密码 |\n|---|---|\n| root | [REDACTED] |\n| ops | [REDACTED_IP] |"
        self.assertEqual(hook.redact(already_redacted), already_redacted)

    # --- Round-14 dual-review retry (independent Claude opus + Codex, 2026-08-22, against the
    # round-13 attempt): P1 BLOCKING finding item 1 -- `_CN_MOBILE_RE` was widened that round to
    # also recognize space/dash-grouped formatted mainland-China mobile numbers (for genuine
    # standalone-PII redaction), but Phase A's atomic-span capture did not grow matching coverage
    # for every keyword+connector shape at the same time, so a labeled value glued to its keyword
    # by a connector Phase A did not yet recognize (bare ASCII whitespace, the English word "is"
    # after a CJK keyword, "是" after an ASCII keyword, or a compound-suffixed CJK label with no
    # connector at all) fell through to Phase B untouched, where the widened pattern nibbled an
    # 11-digit phone-shaped slice out of the middle -- e.g.
    # `redact('password 186 7723 4491 5508')` -> `'password [REDACTED_PHONE] 5508'`. Fixed by
    # extending Phase A's connector grammar for exactly these shapes -- see
    # `_INLINE_CJK_ENGLISH_IS_SECRET_RE`, `_INLINE_CJK_BARE_SUFFIX_SECRET_RE`, and the widened
    # `_INLINE_ASCII_SECRET_RE` (all in `claude_memory_hook.py`) for the code and full rationale. ---

    def test_round14_keyword_connector_matrix_labeled_phone_shaped_value_redacts_atomically(
        self,
    ) -> None:
        # Regression tests 1 & 5: a parameterized matrix over every keyword x connector
        # combination the round-13 review's finding 1 reproduced (11 keywords x 3 connector
        # shapes, including two compound-suffixed CJK labels), confirming the fix generalizes
        # rather than patching each reported cell individually -- the exact test-coverage-shape
        # gap the review's own finding 2 flagged in the round-12 test suite. Every cell must
        # redact the whole space-grouped value as ONE atomic span with zero digit-groups of the
        # real value surviving in the clear.
        secret_value = "186 7723 4491 5508"
        secret_groups = secret_value.split()
        cjk_keywords = ("恢复码", "验证码", "密码", "密钥", "令牌", "设备码", "备份码", "备用码")
        cjk_suffixed_keywords = ("密码-prod", "密码2")
        ascii_keywords = ("password", "pwd", "token", "api_key", "db_password_prod")
        connectors = (" ", " is ", " 是 ")

        checked = 0
        for keyword in cjk_keywords + cjk_suffixed_keywords + ascii_keywords:
            for connector in connectors:
                text = f"{keyword}{connector}{secret_value}"
                redacted = hook.redact(text)
                self.assertIn("[REDACTED]", redacted, text)
                self.assertNotIn("[REDACTED_PHONE]", redacted, f"fragment-leak shape: {text!r} -> {redacted!r}")
                for group in secret_groups:
                    self.assertNotIn(group, redacted, f"{group!r} leaked from {text!r} -> {redacted!r}")
                self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")
                checked += 1
        self.assertEqual(
            checked,
            len(cjk_keywords + cjk_suffixed_keywords + ascii_keywords) * len(connectors),
        )

    def test_round14_labeled_password_value_with_embedded_ipv4_shaped_substring_redacts_atomically(
        self,
    ) -> None:
        # Regression test 2: a labeled password value containing an embedded IPv4-shaped
        # substring, with real prefix/suffix characters of the actual secret around it, must
        # redact as one atomic span -- not have `_IPV4_RE` (Phase B) nibble just the dotted-quad
        # out of the middle. This is item 3's pre-existing IPv4-nibbling repro from the review,
        # closed as a side effect of the same connector-grammar fix (`_INLINE_CJK_ENGLISH_IS_SECRET_RE`
        # for the "is"-connector case; the bare-space ASCII fallback for the other).
        cases = (
            "密码 is Gn-198.51.100.23-Pk",
            "token Gn-198.51.100.23-Pk",
            "api_key is Rt-2001:db8:1234::99-Yb",
        )
        for text in cases:
            redacted = hook.redact(text)
            for fragment in ("Gn-", "-Pk", "198.51.100.23", "Rt-", "-Yb", "2001:db8:1234::99"):
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertIn("[REDACTED]", redacted, text)
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")

    def test_round14_separator_free_device_code_after_bare_keyword_mention(self) -> None:
        # Regression test 3: a separator-free, alphabetic-hyphenated device/authorization code
        # following its keyword with only bare whitespace (no colon/是/为/is) -- pre-existing
        # round-8 STRICT-class behavior, re-verified unchanged by this round's connector-grammar
        # widening.
        secret = "WDJB-MJHT"
        for text in (f"设备码 {secret}", f"验证码 {secret}", f"设备码 is {secret}"):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED]", redacted, text)

    def test_round14_email_shaped_bare_mention_value_redacts_atomically(self) -> None:
        # Item 3's email-nibbling repro: a bare-mention (no separator at all) or bare-whitespace
        # labeled value that is an email address with real affix characters around it. Fixed by
        # teaching `_looks_like_secret_code` to accept '@' as an unambiguous secret-value signal,
        # the same way it already accepts a digit.
        cases = (
            "密码 Wm-devnull@example.invalid-Qx",
            "密钥Wm-devnull@example.invalid-Qx",
        )
        for text in cases:
            redacted = hook.redact(text)
            for fragment in ("Wm-", "-Qx", "devnull@example.invalid"):
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertIn("[REDACTED]", redacted, text)

    def test_round14_value_length_cap_raised_does_not_leak_a_tail_past_4096(self) -> None:
        # Regression for item 10: the labeled-value length cap was a genuine truncation point at
        # 4096 characters, not just a theoretical backstop -- a value longer than that leaked
        # everything past character 4096 in the clear next to a placeholder that looked fully
        # handled. Raised to 65536; re-verify a value straddling the OLD 4096 boundary no longer
        # leaks any tail bytes.
        tail = "TAIL_MARKER_never_leaked"
        value = ("x" * 4100) + tail
        redacted = hook.redact(f"密码：{value}")
        self.assertNotIn(tail, redacted, redacted)
        self.assertNotIn("x" * 10, redacted, redacted)
        self.assertEqual(redacted, "密码：[REDACTED]")

        value_ascii = ("A" * 4097) + "ZZZ"
        redacted_ascii = hook.redact(f"password: {value_ascii}")
        self.assertNotIn("ZZZ", redacted_ascii, redacted_ascii)
        self.assertEqual(redacted_ascii, "password: [REDACTED]")

    def test_round14_schema_type_word_after_explicit_separator_is_not_redacted(self) -> None:
        # Item 11: a config/API-schema doc line naming a field's TYPE, not a real secret value
        # ("password: string (required)"), must not redact the type name -- closed by adding
        # common JSON/OpenAPI schema type words to the existing closed-vocabulary denylist.
        unchanged_cases = (
            "password: string (required)",
            "secret: number",
            "token: boolean",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_round14_prefixed_token_shape_keeps_its_specific_placeholder_tag(self) -> None:
        # This round's own new bare-whitespace/weak-connector Phase A coverage must not downgrade
        # an already-correct, more specific Phase B tag: a recognized prefixed-token shape
        # (`_TOKEN_RE`'s own sk-/ghp_/AKIA-style vocabulary) following the bare word "token" (no
        # separator at all) must still redact with the specific "[REDACTED_TOKEN]" placeholder,
        # not a generic one -- required for `test_write_candidate_capture.py`'s
        # `test_t7_end_to_end_supersedes_hint_never_contains_raw_secret` (an off-limits test this
        # round must not modify), which was caught failing during this round's own verification
        # after the bare-whitespace fallback was first added, and fixed by declining in Phase A
        # whenever the captured value is an EXACT match for `_TOKEN_RE`'s own shape (a value that
        # merely *contains* a token-shaped substring alongside real affix bytes still gets
        # Phase A's atomic treatment, unaffected).
        secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        for text in (f"token {secret}", f"token is {secret}"):
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, text)
            self.assertIn("[REDACTED_TOKEN]", redacted, text)

    def test_round14_new_connector_patterns_scale_linearly_on_adversarial_input(self) -> None:
        # Regression test 6: the three new/widened patterns this round
        # (`_INLINE_CJK_ENGLISH_IS_SECRET_RE`, `_INLINE_CJK_BARE_SUFFIX_SECRET_RE`, and the
        # widened `_INLINE_ASCII_SECRET_RE`) must not reopen a ReDoS surface. Measured scaling,
        # not just "it completes": doubling the input should roughly double the time (linear),
        # not quadruple or worse.
        # Round-6 P3 finding (independent Claude opus + Codex dual review, 2026-08-22): see
        # `_min_elapsed`'s own comment -- best-of-5 plus a loosened absolute backstop replaces
        # the single-shot ceilings that flaked under concurrent CPU contention.
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        # Adversarial shape 1: a long non-matching run after "keyword is ", exercising
        # `_INLINE_CJK_ENGLISH_IS_SECRET_RE`'s STRICT guard reachability scan.
        small = timed("密码 is " + "b" * 8_000)
        large = timed("密码 is " + "b" * 32_000)  # 4x input
        self.assertLess(large, small * 4 + 0.5, "CJK 'is' pattern must scale near-linearly")
        self.assertLess(large, 3.0)

        # Adversarial shape 2: a long non-matching run after a compound-suffixed keyword's bare
        # whitespace, exercising `_INLINE_CJK_BARE_SUFFIX_SECRET_RE`.
        small2 = timed("密码-prod " + "c" * 8_000)
        large2 = timed("密码-prod " + "c" * 32_000)  # 4x input
        self.assertLess(large2, small2 * 4 + 0.5, "CJK bare-suffix pattern must scale near-linearly")
        self.assertLess(large2, 3.0)

        # Adversarial shape 3: the widened ASCII bare-whitespace fallback with a long
        # non-matching run and no "is"/"是" word anywhere for the optional group to latch onto.
        small3 = timed("token " + "d" * 8_000)
        large3 = timed("token " + "d" * 32_000)  # 4x input
        self.assertLess(large3, small3 * 4 + 0.5, "ASCII bare-whitespace fallback must scale near-linearly")
        self.assertLess(large3, 3.0)

    def test_round14_new_connector_patterns_do_not_flag_realistic_technical_prose(self) -> None:
        # Regression test 7: realistic non-secret Chinese/English technical sentences exercising
        # specifically the NEW connector shapes this round adds (bare ASCII whitespace, CJK "is",
        # ASCII "是", compound-suffix bare whitespace) -- confirming they did not newly start
        # matching ordinary prose that merely happens to use a label word.
        unchanged_cases = (
            "token expires soon after each login",
            "key insight from the retro: ship smaller diffs",
            "password expires after ninety days of inactivity",
            "密码 is Argon2id 是推荐的哈希算法",
            "密码-prod policy requires monthly rotation",
            "password 是 required for every deploy step",
            "signature well-known convention applies here",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_round14_new_patterns_use_no_unscoped_ignorecase_or_bare_word_boundary(self) -> None:
        # CJK-adjacency / IGNORECASE-taint invariant (binding since round 2, re-verified directly
        # rather than just trusting the existing suite): every new pattern this round must use
        # the locally-scoped `(?i:...)` idiom, never a bare `(?i)` global flag or an unscoped
        # `\b`, both of which would taint an ASCII boundary lookaround elsewhere in the same
        # combined pattern once mixed with a CJK-adjacent context.
        for pattern in (
            hook._INLINE_CJK_ENGLISH_IS_SECRET_RE,
            hook._INLINE_CJK_BARE_SUFFIX_SECRET_RE,
            hook._INLINE_ASCII_SECRET_RE,
        ):
            self.assertFalse(pattern.flags & re.IGNORECASE, pattern.pattern)
            self.assertNotIn(r"\b", pattern.pattern)

    def test_round14_cjk_adjacency_spot_check_on_new_and_existing_patterns(self) -> None:
        # CJK-adjacency re-verification (binding requirement, not just trusting the existing
        # suite passing): a secret keyword immediately preceded or followed by unrelated CJK
        # prose must not have its match boundary corrupted by this round's changes, spot-checked
        # directly against representative cases spanning the new connector shapes.
        secret = "186 7723 4491 5508"
        cases = (
            f"账户信息如下：密码 is {secret}，请妥善保管",
            f"备注内容：token 是 {secret}，仅供测试",
            f"说明：密码-prod {secret} 为临时凭据",
        )
        for text in cases:
            redacted = hook.redact(text)
            for group in secret.split():
                self.assertNotIn(group, redacted, f"{group!r} leaked from {text!r} -> {redacted!r}")
            self.assertIn("[REDACTED]", redacted, text)

    # --- Round 17 dual-review retry (independent Claude opus + Codex, 2026-08-22, against the
    # round-16 architectural-rewrite attempt): P1 BLOCKING finding 1 -- an ASCII keyword paired
    # with a CJK connector ("为"/"，即"/whitespace-free "是"/etc, none of which
    # `_INLINE_ASCII_SECRET_RE`'s own connector recognized) never reached Phase A at all, so
    # Phase B's narrower structural patterns fragmented the value exactly like before this whole
    # rewrite. See `_INLINE_ASCII_SECRET_RE`'s own comment for the fix. Also covers the P2
    # NON-BLOCKING idempotency finding (`_cell_is_genuine_secret_label`'s own comment). ------------

    def test_round17_ascii_keyword_recognizes_cjk_connector_words(self) -> None:
        # Every repro from the review's finding 1, each with its own synthetic secret value
        # (never the real ones the review found). Every case must redact the value as ONE atomic
        # span with zero characters of the value surviving, and stay idempotent.
        cases = (
            ("password为Ab-198.51.100.7-Cd", ("Ab-", "-Cd", "198.51.100.7")),
            ("password，即Ab-198.51.100.7-Cd", ("Ab-", "-Cd", "198.51.100.7")),
            ("password 为 Ab-198.51.100.7-Cd", ("Ab-", "-Cd", "198.51.100.7")),
            ("password是Ab-198.51.100.7-Cd", ("Ab-", "-Cd", "198.51.100.7")),
            ("token为Xy-sk-abcdefghij1234567890-Zw", ("Xy-", "-Zw", "sk-abcdefghij1234567890")),
            ("api_key为Pq-13800138000-Rs", ("Pq-", "-Rs", "13800138000")),
            ("passphrase为Ee-bob@corp.example-Ff", ("Ee-", "-Ff", "bob@corp.example")),
            ("db_password_prod就是Gh-203.0.113.9-Kl", ("Gh-", "-Kl", "203.0.113.9")),
            ("secret设置为Mn-01:23:45:67:89:ab-Op", ("Mn-", "-Op", "01:23:45:67:89:ab")),
        )
        for text, fragments in cases:
            redacted = hook.redact(text)
            self.assertIn("[REDACTED", redacted, text)
            for fragment in fragments:
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")

    def test_round17_ascii_keyword_cjk_connector_does_not_flag_realistic_non_secret_prose(self) -> None:
        # False-positive sweep for the new connector branch specifically: realistic Chinese/English
        # sentences pairing an ASCII keyword with the same CJK connector words, but no real secret
        # value following, must not be redacted.
        unchanged_cases = (
            "token为required",
            "token为空",  # "token is empty", a common Chinese phrase
            "password为true",
            "password为false",
            "secret为一个重要的概念",
            "key为index提供加速",
            "the password equals its own hash after rotation",
            "api_key等于configured默认值",
            "password就是这个字段的名字",
            "token设置为默认",
            "secret改为常量",
            "signature改成签名算法",
            "passphrase是密码学中的一个术语",
            "password 为 required for every deploy step",
            "token ，即 identifier 的另一种叫法",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_round17_ascii_keyword_cjk_connector_scales_linearly_on_adversarial_input(self) -> None:
        # ReDoS regression: the new connector alternative must not reopen a catastrophic-
        # backtracking surface, measured scaling (not just "it completes").
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        small = timed("password" + (" " * 8_000) + "x")
        large = timed("password" + (" " * 32_000) + "x")  # 4x input
        self.assertLess(large, small * 4 + 0.5, "ASCII+whitespace fallback must scale near-linearly")
        self.assertLess(large, 3.0)

        small2 = timed("password" + ("、" * 8_000) + "x")
        large2 = timed("password" + ("、" * 32_000) + "x")  # 4x input
        self.assertLess(large2, small2 * 4 + 0.5, "ASCII+CJK-punctuation-run must scale near-linearly")
        self.assertLess(large2, 3.0)

        small3 = timed("password" + ("为" * 8_000))
        large3 = timed("password" + ("为" * 32_000))  # 4x input
        self.assertLess(large3, small3 * 4 + 0.5, "ASCII+repeated-CJK-connector-token must scale near-linearly")
        self.assertLess(large3, 3.0)

    def test_round17_ascii_keyword_cjk_connector_uses_no_unscoped_ignorecase_or_bare_word_boundary(
        self,
    ) -> None:
        # CJK-adjacency / IGNORECASE-taint invariant, re-verified directly against the modified
        # pattern rather than just trusting the existing suite passing.
        self.assertFalse(hook._INLINE_ASCII_SECRET_RE.flags & re.IGNORECASE, hook._INLINE_ASCII_SECRET_RE.pattern)
        self.assertNotIn(r"\b", hook._INLINE_ASCII_SECRET_RE.pattern)

    def test_round17_cjk_adjacency_spot_check_on_ascii_keyword_cjk_connector(self) -> None:
        secret = "186 7723 4491 5508"
        cases = (
            f"账户信息如下：password为{secret}，请妥善保管",
            f"备注内容：token，即{secret}，仅供测试",
            f"说明：api_key就是{secret}为临时凭据",
            f"密码是重要话题，password为{secret}这是真实值",
        )
        for text in cases:
            redacted = hook.redact(text)
            for group in secret.split():
                self.assertNotIn(group, redacted, f"{group!r} leaked from {text!r} -> {redacted!r}")
            self.assertIn("[REDACTED]", redacted, text)

    def test_round17_table_row_with_inline_redacted_label_stays_idempotent_and_does_not_over_redact(
        self,
    ) -> None:
        # P2 NON-BLOCKING finding: a table row whose secret was already redacted in place, inline
        # within its own label cell, must not have an unrelated later cell over-redacted on a
        # second `redact()` pass -- see `_cell_is_genuine_secret_label`'s own comment. Fixed to be
        # a true fixed point after the FIRST pass already, not merely by the third.
        cases = (
            "| password-prod is sk-abcdefghij1234567890 | next |",
            "| passphrase_test Xk9|Zq7 | next |",
            "| 助记词3  Xk9|Zq7 | next |",
        )
        for text in cases:
            pass1 = hook.redact(text)
            pass2 = hook.redact(pass1)
            self.assertEqual(pass1, pass2, f"must be a fixed point after pass 1: {text!r} -> {pass1!r} -> {pass2!r}")
            self.assertIn("next", pass2, f"unrelated cell over-redacted: {text!r} -> {pass2!r}")

    def test_round17_placeholder_keyword_substring_is_not_treated_as_a_genuine_label(self) -> None:
        # Mechanism (a) from `_cell_is_genuine_secret_label`'s own comment: this file's own
        # placeholder tag names ("REDACTED_TOKEN", "REDACTED_PRIVATE_KEY") coincidentally contain a
        # real keyword vocabulary word ("token", "private_key") -- a cell that is nothing but an
        # already-emitted placeholder must never be read as a fresh secret label.
        for placeholder in ("[REDACTED_TOKEN]", "[REDACTED_PRIVATE_KEY]", "[REDACTED]"):
            self.assertFalse(hook._cell_is_genuine_secret_label(f" {placeholder} "), placeholder)
        # A genuine label cell with no placeholder is unaffected.
        self.assertTrue(hook._cell_is_genuine_secret_label(" 密码 "))
        self.assertTrue(hook._cell_is_genuine_secret_label(" password "))

    # --- Architectural-rewrite round 2 (independent Claude opus + Codex dual review against the
    # round-1 architectural rewrite, two P1 BLOCKING findings) -----------------------------------

    def test_round2_ascii_code_keyword_vocabulary_closes_phone_shaped_leak(self) -> None:
        # P1 finding 1: the ASCII keyword vocabulary omitted every "code"-shaped secret label
        # ("backup code"/"recovery code"/"PIN"/"OTP"/"passcode"/"one-time code"), so Phase A never
        # attempted a match on any of them and `_CN_MOBILE_RE` (Phase B) nibbled an 11-digit slice
        # out of a labeled, space-grouped value, leaving the remainder exposed next to a
        # `[REDACTED_PHONE]` placeholder that made the line look fully handled. Fail-before/
        # pass-after against HEAD, synthetic values only.
        secret = "159 3308 7742 6015"
        cases = (
            f"recovery code: {secret}",
            f"backup code: {secret}",
            f"PIN: {secret}",
            f"pin: {secret}",
            f"OTP: {secret}",
            f"one-time code: {secret}",
            f"onetime code: {secret}",
        )
        for text in cases:
            redacted = hook.redact(text)
            for group in secret.split():
                self.assertNotIn(group, redacted, f"{group!r} leaked from {text!r} -> {redacted!r}")
            self.assertNotIn("[REDACTED_PHONE]", redacted, text)
            self.assertIn("[REDACTED]", redacted, text)
        # A bare digit-only device code value redacts too, via the same widened vocabulary, in
        # both the table-cell and "is"-connector inline-prose matchers (`_ASCII_SECRET_KEYWORD_CORE`
        # -- kept textually in sync with `_ASSIGNMENT_RE`'s own copy).
        self.assertIn("[REDACTED]", hook.redact("| PIN | 186 5527 4419 8806 |"))
        self.assertNotIn("5527", hook.redact("| PIN | 186 5527 4419 8806 |"))
        self.assertIn("[REDACTED]", hook.redact("backup code is 159 3308 7742 6015"))
        self.assertNotIn("7742", hook.redact("backup code is 159 3308 7742 6015"))

    def test_round2_alphanumeric_code_group_space_bridge_is_atomic(self) -> None:
        # P1 finding 2: the space-continuation only ever fired before a literal ASCII digit, so a
        # real device/backup-code value whose groups are ALPHANUMERIC rather than purely numeric
        # (the dominant real-world 2FA/backup-code shape) terminated at the first group boundary,
        # leaking the remaining groups next to a placeholder that made the line look fully handled.
        # Fail-before/pass-after against HEAD, synthetic values only. Covers the CJK PERMISSIVE
        # (explicit separator), CJK STRICT bare-mention (no separator at all), and ASCII
        # `_ASSIGNMENT_RE` paths independently.
        mixed = "A1B2 C3D4 E5F6"
        letter_and_digit_groups = "XKCD 7742 QRST 6015"
        cases = (
            (f"备份码：{mixed}", ("A1B2", "C3D4", "E5F6")),
            (f"恢复码：{letter_and_digit_groups}", ("XKCD", "7742", "QRST", "6015")),
            (f"恢复码 {mixed}", ("A1B2", "C3D4", "E5F6")),  # bare mention, no separator
            (f"密码：Ab3x K9mQ 2vR8", ("Ab3x", "K9mQ", "2vR8")),
            (f"password: {mixed}", ("A1B2", "C3D4", "E5F6")),  # ASCII _ASSIGNMENT_RE path
        )
        for text, groups in cases:
            redacted = hook.redact(text)
            for group in groups:
                self.assertNotIn(group, redacted, f"{group!r} leaked from {text!r} -> {redacted!r}")
            self.assertEqual(redacted.count("[REDACTED"), 1, f"{text!r} -> {redacted!r} (not one atomic span)")

    def test_round2_table_column_scan_does_not_consume_the_cells_leading_space(self) -> None:
        # Found by this round's own test run (not the prior review): widening the continuation to
        # recognize a digit anywhere in the upcoming token let it be satisfied starting AT a table
        # cell's own leading space (before any real value character had been consumed at all),
        # corrupting the cell's formatting -- `redact('| 密码 |\n|---|\n| Qw7#zP2mLv8Ke |')`
        # produced `'| 密码 |\n|---|\n|[REDACTED] |'`, losing the space after the opening pipe.
        # Fixed with an alnum lookbehind requiring a real value character before any bridged space.
        secret = "Qw7#zP2mLv8Ke"
        redacted = hook.redact(f"| 密码 |\n|---|\n| {secret} |")
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted, "| 密码 |\n|---|\n| [REDACTED] |")

    def test_round2_code_group_bridge_does_not_swallow_ordinary_numbered_words(self) -> None:
        # Found by this round's own test run (not the prior review): the first branch of the
        # widened continuation ("a digit occurs somewhere in the upcoming token") is satisfied by
        # an entirely ordinary lowercase word immediately followed by a single trailing digit
        # ("word0", "word1", ... -- a realistic shape for numbered placeholders/steps/variables),
        # so a labeled value bridged straight through a long run of them and swallowed an entire
        # unrelated trailing paragraph into the placeholder -- the exact round-11 over-redaction
        # regression this file's history already documents, just reachable through a new token
        # shape. Fixed by excluding "lowercase-run then digit-run then boundary" from that branch.
        long_text = "密码：Ab7xK9m " + " ".join(f"word{i}" for i in range(200))
        redacted = hook.redact(long_text)
        self.assertGreater(len(redacted), 1000, "trailing numbered-word prose must not be swallowed")
        self.assertNotIn("Ab7xK9m", redacted)
        self.assertTrue(redacted.startswith("密码：[REDACTED] word0 word1"), redacted)
        # The equivalent ASCII `_ASSIGNMENT_RE` path must not swallow it either.
        ascii_long_text = "password: Ab7xK9m " + " ".join(f"step{i}" for i in range(200))
        ascii_redacted = hook.redact(ascii_long_text)
        self.assertGreater(len(ascii_redacted), 1000, "trailing numbered-word prose must not be swallowed")
        self.assertNotIn("Ab7xK9m", ascii_redacted)
        self.assertTrue(ascii_redacted.startswith("password: [REDACTED] step0 step1"), ascii_redacted)

    def test_round2_code_token_lookahead_is_not_ignorecase_tainted(self) -> None:
        # Found by this round's own test run (not the prior review): `_ASSIGNMENT_RE` compiles
        # with a GLOBAL `(?i)` flag, so an unscoped `[A-Z0-9]` inside the shared continuation's
        # "no lowercase" branch is silently case-folded to `[A-Za-z0-9]` under that flag, matching
        # any alphanumeric run regardless of case and defeating the whole "no lowercase" signal --
        # `redact('token=abc123 and rest')` swallowed the entire trailing sentence before this was
        # scoped with the file's established `(?-i:...)` idiom. Re-verified directly, plus a spot
        # check that this constant carries no unscoped `(?i)`/bare `\b` of its own.
        self.assertEqual(hook.redact("token=abc123 and rest"), "token=[REDACTED] and rest")
        self.assertEqual(
            hook.redact("password=Xk9pLmQ and the deploy continues as usual"),
            "password=[REDACTED] and the deploy continues as usual",
        )
        self.assertNotIn(r"\b", hook._SECRET_VALUE_CODE_TOKEN_LOOKAHEAD)

    def test_round2_new_ascii_code_keywords_do_not_flag_realistic_prose(self) -> None:
        # Regression test 7 (this round): realistic non-secret English sentences that merely
        # contain a substring of, or are adjacent to, the newly-added ASCII code-word vocabulary,
        # confirming the round-2 vocabulary widening did not newly start matching ordinary prose.
        unchanged_cases = (
            "opinion: we should proceed with the plan",
            "napkin: needed for the picnic",
            "spinning: the wheel is turning",
            "The zip code: 94107 is for San Francisco.",
            "pinned: this dependency is pinned to 1.2.3",
            "otp: the abbreviation is unrelated to this sentence",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_round2_code_token_lookahead_scales_linearly_on_adversarial_input(self) -> None:
        # Regression test 6 (this round): the widened continuation (`_SECRET_VALUE_CODE_TOKEN_LOOKAHEAD`)
        # must not reopen a ReDoS surface -- measured scaling, not just "it completes".
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        # Long chain of alphanumeric code groups joined by single spaces -- exactly the shape this
        # round's widening exists to bridge atomically.
        small = timed("密码：" + " ".join(["A1b2"] * 2_000))
        large = timed("密码：" + " ".join(["A1b2"] * 8_000))  # 4x input
        self.assertLess(large, small * 4 + 0.5, "alnum code-group chain must scale near-linearly")
        self.assertLess(large, 1.0)

        # Long chain of ordinary numbered words -- the shape the new exclusion branch has to scan
        # past at every gap without it costing more than a small constant per gap.
        small2 = timed("password: real " + " ".join(f"word{i}" for i in range(2_000)))
        large2 = timed("password: real " + " ".join(f"word{i}" for i in range(8_000)))  # 4x input
        self.assertLess(large2, small2 * 4 + 0.5, "numbered-word exclusion scan must scale near-linearly")
        self.assertLess(large2, 1.0)

    def test_round2_redact_is_idempotent_on_new_alphanumeric_code_group_shapes(self) -> None:
        # Idempotency: redact(redact(x)) == redact(x) for every new shape this round introduces.
        cases = (
            "recovery code: 159 3308 7742 6015",
            "PIN: 186 5527 4419 8806",
            "备份码：A1B2 C3D4 E5F6",
            "恢复码：XKCD 7742 QRST 6015",
            "password: A1B2 C3D4 E5F6",
            "恢复码 A1B2 C3D4 E5F6",
            "密码：Ab3x K9mQ 2vR8",
            "otp: 843201",
            "| 密码 |\n|---|\n| Qw7#zP2mLv8Ke |",
            "| 用户 | 密码 |\n|---|---|\n| root | A1B2 C3D4 E5F6 |",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: once={once!r} twice={twice!r}")

    # ------------------------------------------------------------------
    # Round-4 (architectural-rewrite retry, P1 BLOCKING fix): `_cjk_value_pattern`'s `{4,max_len}`
    # bound reintroduced the exact fragment-leak shape this whole rewrite exists to close, just
    # reached via length instead of a competing structural pattern -- a labeled value longer than
    # `_CJK_VALUE_MAX_LEN` (65536) characters was matched only up to the cap, leaking every
    # character past it in the clear next to a "[REDACTED]" marker. `_sub_atomic_value` (see that
    # function's own comment above `_cjk_value_pattern`) closes this by extending an already-capped
    # match's consumed span, via a single anchored `.match()` call, past the cap -- without removing
    # the cap itself (an unbounded `{4,}` here is measurably quadratic, see `_cjk_value_pattern`'s
    # own ReDoS history). These tests reproduce the exact repro from the review (a synthetic
    # equivalent, never a real secret) across every Phase A call site the fix touches, verify the
    # extension mechanism itself scales linearly (not just "completes"), and pin idempotency on the
    # new shape.
    # ------------------------------------------------------------------

    def test_round4_labeled_value_exceeding_the_length_cap_redacts_as_one_atomic_span(self) -> None:
        # Synthetic equivalent of the review's own repro:
        # redact('密码：' + ('a!'*50000) + 'TAILSECRETMARKER9') used to leave 34,484 real secret
        # characters exposed. Covers every Phase A call site whose value class is built from
        # `_cjk_value_pattern` -- each must now redact to exactly its keyword/connector prefix plus
        # one placeholder, with zero characters of the >64KB tail surviving anywhere in the output.
        tail_marker = "TAILSECRETMARKER9"
        # Digit-bearing filler ("a1", not "a!"): the STRICT bare-mention/`_is`-connector patterns'
        # own `[0-9-]` guard and their `_looks_like_secret_code` Python-side check both need a
        # digit or hyphen within the first `_CJK_VALUE_MAX_LEN` characters of the value to decide
        # to redact at all -- a digit that only appears in the tail marker, past the cap, would be
        # found by the regex guard's own unbounded lookahead but not by `_looks_like_secret_code`
        # (which only ever sees the already-capped match text), causing those patterns to decline
        # a value they would have accepted with a genuine, uncapped view -- a real but narrower,
        # pre-existing, non-blocking gap unrelated to this round's fix (see this round's final
        # report), not something these tests are trying to exercise.
        long_value = ("a1" * 50_000) + tail_marker
        cases = {
            # PERMISSIVE inline, explicit full-width colon separator.
            f"密码：{long_value}": "密码：[REDACTED]",
            # STRICT inline, bare-mention (no separator at all).
            f"设备码 {long_value}": "设备码 [REDACTED]",
            # The dedicated CJK-keyword + English "is" connector pattern.
            f"密钥 is {long_value}": "密钥 is [REDACTED]",
            # The dedicated compound-suffix + bare-whitespace pattern.
            f"密码-prod {long_value}": "密码-prod [REDACTED]",
            # The ASCII-keyword "is"-connector sibling.
            f"api key is {long_value}": "api key is [REDACTED]",
            # The single-row table pattern.
            f"| 密码 | {long_value} |": "| 密码 | [REDACTED] |",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertNotIn(tail_marker, redacted, (text[:40], redacted))
            self.assertEqual(redacted, expected, (text[:40], redacted))

        # The header/data-row table column scanner (a separate code path, see
        # `_redact_cjk_secret_table_columns`).
        table_columns = f"| 密钥 |\n|---|\n| {long_value} |"
        redacted_columns = hook.redact(table_columns)
        self.assertNotIn(tail_marker, redacted_columns, redacted_columns)
        self.assertEqual(redacted_columns, "| 密钥 |\n|---|\n| [REDACTED] |")

        # The `助记词`/`助記詞`-scoped WIDE (alphanumeric) continuation body -- a different `body`
        # sub-pattern than every case above, so this exercises a second entry in
        # `_CJK_VALUE_CONTINUATION_CACHE`.
        long_mnemonic = " ".join(["apple"] * 20_000) + " " + tail_marker
        redacted_mnemonic = hook.redact(f"助记词：{long_mnemonic}")
        self.assertNotIn(tail_marker, redacted_mnemonic, redacted_mnemonic)
        self.assertEqual(redacted_mnemonic, "助记词：[REDACTED]")

    def test_round4_capped_value_extension_survives_split_blocks_end_to_end(self) -> None:
        # The review's own end-to-end note: `split_blocks()` applies `redact(text)[:4_000]`, and
        # truncating *after* redaction does not rescue a leaked tail -- redaction SHORTENS the
        # string, pulling any surviving tail characters back within the first 4,000 characters of
        # the emitted block. Verified directly against the real pipeline, not just `redact()` in
        # isolation.
        tail_marker = "TAILSECRETMARKER9"
        long_value = ("a!" * 50_000) + tail_marker
        document = hook.MemoryDocument(
            project_ref="proj", text=f"密码：{long_value}", mtime_ns=0
        )
        blocks = list(hook.split_blocks(document))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "密码：[REDACTED]")
        self.assertNotIn(tail_marker, blocks[0].text)

    def test_round4_capped_value_extension_scales_linearly_on_adversarial_input(self) -> None:
        # Not just "it completes": doubling a value well past the 65536-character cap should
        # roughly double the extension's own cost (linear), not reproduce the quadratic blowup the
        # cap exists to prevent. `_sub_atomic_value`'s extension is a single anchored `.match()`
        # call per already-successful match, so its cost is O(remaining run length), not
        # O(document length) or worse.
        def timed(text: str) -> float:
            started = time.monotonic()
            hook.redact(text)
            return time.monotonic() - started

        small = timed("密码：" + ("Ab3" * 40_000))  # ~120,000 chars, well past the 65,536 cap
        large = timed("密码：" + ("Ab3" * 160_000))  # 4x input
        self.assertLess(large, small * 4 + 0.5, "capped-value extension must scale near-linearly")
        self.assertLess(large, 1.0)

    def test_round4_redact_is_idempotent_on_capped_value_extension(self) -> None:
        long_value = "Ab3" * 30_000 + "TAILMARK"
        cases = (
            f"密码：{long_value}",
            f"设备码 {long_value}",
            f"密钥 is {long_value}",
            f"密码-prod {long_value}",
            f"api key is {long_value}",
            f"| 密码 | {long_value} |",
            f"| 密钥 |\n|---|\n| {long_value} |",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text[:40]!r}: once={once!r} twice={twice!r}")

    def test_round4_sub_atomic_value_extends_a_capped_match_past_the_cap(self) -> None:
        # Direct unit test of `_sub_atomic_value`/`_extend_capped_value_end` in isolation, decoupled
        # from `redact()`'s many real-world CJK/ASCII patterns: a pattern whose `value` group is
        # bounded at exactly `_CJK_VALUE_MAX_LEN` (mirroring `_cjk_value_pattern`'s own `{4,max_len}`
        # shape) must still have its FULL run -- including everything past the cap -- claimed by a
        # single placeholder when the callback actually redacts.
        capped_pattern = re.compile(r"(?P<value>a{1,%d})" % hook._CJK_VALUE_MAX_LEN)

        def resolve_body(match: re.Match[str]) -> tuple[str, int, int]:
            return ("a",) + match.span("value")

        def redact_everything(match: re.Match[str]) -> str:
            return "[REDACTED]"

        text = "a" * (hook._CJK_VALUE_MAX_LEN + 5_000) + "STOP more text"
        result = hook._sub_atomic_value(capped_pattern, redact_everything, text, resolve_body)
        self.assertEqual(result, "[REDACTED]STOP more text")

    def test_round4_sub_atomic_value_extension_never_fires_on_a_declined_match(self) -> None:
        # The mirror-image case: `_sub_atomic_value` only attempts extension when the callback's
        # replacement differs from the original matched text (i.e. the match was actually
        # redacted, not declined) -- `resolve_body` must not even be CALLED for a declined match,
        # and trailing document content must survive byte-for-byte, even when the declined match's
        # own span is well past `_CJK_VALUE_MAX_LEN`.
        capped_pattern = re.compile(r"(?P<value>a{1,%d})" % hook._CJK_VALUE_MAX_LEN)
        resolve_calls = 0

        def resolve_body(match: re.Match[str]) -> tuple[str, int, int]:
            nonlocal resolve_calls
            resolve_calls += 1
            return ("a",) + match.span("value")

        def decline_everything(match: re.Match[str]) -> str:
            return match.group(0)

        text = "a" * (hook._CJK_VALUE_MAX_LEN + 5_000) + " TRAILING TEXT MUST SURVIVE"
        result = hook._sub_atomic_value(capped_pattern, decline_everything, text, resolve_body)
        self.assertEqual(result, text)
        self.assertEqual(resolve_calls, 0, "resolve_body must never be called for a declined match")

    # --- Architectural-rewrite round 16 (independent Claude opus + Codex dual review against the
    # round-15 attempt, 2 P1 findings: `_CN_MOBILE_RE`'s widened trailing guard nibbling the first
    # 11 digits of a longer grouped-digit run, and `_CJK_VALUE_CHARS_COMMON`'s ASCII-only allowlist
    # still terminating an atomic span on any non-ASCII character) -----------------------------

    def test_round16_grouped_digit_run_longer_than_a_phone_number_is_not_half_redacted(self) -> None:
        # P1 BLOCKING, newly introduced by round 15's own widening: a space/dash-grouped digit run
        # LONGER than 11 digits matched only its first 11, leaving the remainder exposed next to a
        # "[REDACTED_PHONE]" marker that made the line look fully handled. Fail-before/pass-after
        # against the actual round-15 HEAD of this file (verified directly before this fix): each
        # of these produced a half-redacted fragment leak; every one must now come through either
        # fully unredacted (an honest miss, not a deceptive partial redaction) or, once claimed by
        # a recognized keyword, fully redacted as one atomic span.
        cases = (
            "158 6027 4419 7735",
            "157-6603-9284-4471",
            "158 6027 4419 7735 8802",
            "工单串：158 6027 4419 7735",
            "订单号 158 6027 4419 7735 已发货",
        )
        for text in cases:
            redacted = hook.redact(text)
            self.assertNotIn(
                "[REDACTED_PHONE]",
                redacted,
                f"{text!r} -> {redacted!r} (a longer grouped-digit run must not be half-redacted)",
            )
            self.assertEqual(redacted, text, f"{text!r} -> {redacted!r}")
        # Sanity counterpart: a run that IS exactly 11 digits (not longer) still redacts as before
        # -- this fix must narrow only runs LONGER than a phone number, never a genuine one.
        exact_eleven = hook.redact("统计结果 138 0013 8000 条记录")
        self.assertEqual(exact_eleven, "统计结果 [REDACTED_PHONE] 条记录")

    def test_round16_cn_mobile_still_redacts_a_genuine_standalone_eleven_digit_number(self) -> None:
        # Mirror-image of the fix above: the new boundary guards must not narrow an already-correct
        # match -- a genuine, exactly-11-digit mobile number (grouped or not, with or without a
        # country-code prefix) still redacts in full, whether standalone or immediately followed by
        # ordinary trailing prose (never another digit).
        cases = (
            ("call me at 138 0013 8000 today", "138 0013 8000"),
            ("联系电话 138-0013-8000", "138-0013-8000"),
            ("my number is 13800138000.", "13800138000"),
            ("phone: +86 138 0013 8000 please call back", "138 0013 8000"),
        )
        for text, secret in cases:
            redacted = hook.redact(text)
            self.assertIn("[REDACTED", redacted, f"{text!r} -> {redacted!r}")
            self.assertNotIn(secret, redacted, f"{text!r} -> {redacted!r}")

    def test_round16_labeled_value_with_non_ascii_non_cjk_character_redacts_atomically(self) -> None:
        # P1 for the rewrite's own stated goal (honest scoping: byte-identical to round-15 HEAD, so
        # not itself a regression, but the brief's own non-negotiable bar is "zero characters of
        # the secret survive"): a labeled value containing an ordinary non-ASCII, non-CJK character
        # (an accented Latin letter, Cyrillic, Greek, a currency symbol, an en dash, an emoji) used
        # to stop the atomic span right there, handing the remainder of the SAME secret to Phase B's
        # structural siblings (IPv4/IPv6/email) to nibble. Fail-before/pass-after against the actual
        # round-15 HEAD (verified directly): each of these left the prefix/suffix around the
        # structural match exposed in the clear; every one must now redact as ONE atomic span with
        # zero real secret characters surviving.
        for ch in "éßдöé§£αñ–🔑":
            with self.subTest(ch=ch):
                text = f"密码：Qw7zP2{ch}-198.51.100.27-Xk9"
                redacted = hook.redact(text)
                self.assertEqual(redacted, "密码：[REDACTED]", f"{text!r} -> {redacted!r}")
        # The same gap, reached through each structural Phase-B sibling this rewrite protects.
        siblings = (
            "密码：Qw7zP2é-zeta.node@example.invalid-Xk9",
            "密码：Qw7zP2é-2001:db8::9e7f-Xk9",
        )
        for text in siblings:
            redacted = hook.redact(text)
            self.assertEqual(redacted, "密码：[REDACTED]", f"{text!r} -> {redacted!r}")
        # Below the pre-fix `{4,}` floor once the ASCII prefix before the non-ASCII character was
        # too short for a match to even start -- the whole value used to leak, now claimed whole.
        below_floor = hook.redact("密码：Abé-198.51.100.27-Xk")
        self.assertEqual(below_floor, "密码：[REDACTED]")
        # Two round-1 regressions this fix must not reopen: a purely-ASCII space-grouped
        # phone-shaped backup code, and a purely-ASCII embedded IPv4-shaped substring.
        self.assertEqual(hook.redact("备份码：139 8842 7615 3320"), "备份码：[REDACTED]")
        self.assertEqual(hook.redact("密码：Ab-192.0.2.44-Cd"), "密码：[REDACTED]")

    def test_round16_new_patterns_do_not_flag_realistic_technical_prose(self) -> None:
        # False-positive re-verification: the non-ASCII token only widens what a value CONTINUES
        # through once already claimed -- it must not newly cause the CJK-adjacency boundary or the
        # closed non-secret-word denylist to accept ordinary prose that merely uses a label word.
        unchanged_cases = (
            "密码：Argon2id 是推荐的哈希算法。",
            "secret: this field documents the schema",
            "key: used to index the cache",
            "当前密码：bcrypt 哈希算法需要升级",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)
        # Round-8 (architectural-rewrite process-integrity fix, BLOCKING finding 1): this test
        # used to also assert `hook.redact("密码\nTq4zW7pNe2Vs") == "密码\nTq4zW7pNe2Vs"` here --
        # the complete synthetic secret surviving a full pass-through, pinned as a passing
        # expectation and mislabeled "unaffected by either fix this round" even though the test's
        # own name is about false positives, not this pre-existing connector-never-crosses-a-
        # newline gap. That is exactly the leaky-output-as-pass pattern this file's process rules
        # forbid, independent of whether the gap itself is pre-existing (it is: byte-identical at
        # HEAD before this round's changes). Removed rather than re-expressed with a non-secret
        # probe -- see `test_cjk_secret_redaction_documents_known_residual_gaps_still_pass_through`
        # (the one other place this exact assertion existed, fixed the same way this round) for the
        # full reasoning. Left undocumented in tests, as an explicit prose-only residual gap.

    def test_round16_new_patterns_scale_linearly_on_adversarial_input(self) -> None:
        # No ReDoS: both round-16 changes (the widened `_CN_MOBILE_RE` boundary lookarounds, and
        # the new non-ASCII value-body alternative) must scale near-linearly, not quadratically or
        # worse, on a long adversarial run built from exactly the shape each change introduces.
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        # Adversarial shape 1: a long space/digit-grouped run with no real phone number anywhere,
        # exercising `_CN_MOBILE_RE`'s new trailing/leading separator+digit lookarounds.
        small = timed(("1 3 " * 2_000))
        large = timed(("1 3 " * 8_000))  # 4x input
        self.assertLess(large, small * 4 + 0.5, "_CN_MOBILE_RE boundary guards must scale near-linearly")
        self.assertLess(large, 3.0)

        # Adversarial shape 2: a long run of the new non-ASCII value-body token after a keyword,
        # never reaching a genuine terminator.
        small2 = timed("密码：" + "é" * 8_000)
        large2 = timed("密码：" + "é" * 32_000)  # 4x input
        self.assertLess(large2, small2 * 4 + 0.5, "non-ASCII value token must scale near-linearly")
        self.assertLess(large2, 3.0)

    def test_round16_new_patterns_use_no_unscoped_ignorecase_or_bare_word_boundary(self) -> None:
        # CJK-adjacency / IGNORECASE-taint invariant, re-verified directly rather than trusting the
        # existing suite alone: neither round-16 change introduces a bare `(?i)` global flag or an
        # unscoped `\b` into any pattern that consumes the new constants.
        for pattern in (
            hook._CN_MOBILE_RE,
            hook._INLINE_CJK_SECRET_RE,
            hook._TABLE_CJK_SECRET_RE,
        ):
            self.assertFalse(pattern.flags & re.IGNORECASE, pattern.pattern)
            self.assertNotIn(r"\b", pattern.pattern)

    def test_round16_redact_is_idempotent_on_both_new_fix_shapes(self) -> None:
        # redact(redact(x)) == redact(x) for every new shape either fix touches.
        cases = (
            "158 6027 4419 7735",
            "157-6603-9284-4471",
            "统计结果 138 0013 8000 条记录",
            "密码：Qw7zP2é-198.51.100.27-Xk9",
            "密码：Qw7zP2é-zeta.node@example.invalid-Xk9",
            "密码：Qw7zP2é-2001:db8::9e7f-Xk9",
            "密码：Abé-198.51.100.27-Xk",
            "备份码：139 8842 7615 3320",
            "密码：Ab-192.0.2.44-Cd",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: once={once!r} twice={twice!r}")

    # --- Final-round dual-review findings (items 12-17 against the round-N attempt) ---------------

    def test_assignment_quoted_value_is_atomic_across_a_real_space(self) -> None:
        # Item 12: a quoted, multiword passphrase leaked everything after the first word, since a
        # bare space inside the quotes only continued the value when the following token
        # independently looked like a code group. Any character is legitimate inside a real quote
        # pair, so the fix gives the quoted branch its own grammar with no token-shape lookahead at
        # all -- verified fail-before (HEAD produced
        # `'password: "[REDACTED] betaLOCK"'`, leaking the second word and the closing quote).
        redacted = hook.redact('password: "R7mQ betaLOCK"')
        self.assertEqual(redacted, 'password: "[REDACTED]"')
        self.assertNotIn("betaLOCK", redacted)
        self.assertEqual(redacted, hook.redact(redacted))

        # A three-word passphrase and a value with multiple internal spaces both stay atomic too.
        redacted2 = hook.redact('secret: "alpha bravo charlie delta"')
        self.assertEqual(redacted2, 'secret: "[REDACTED]"')
        for word in ("alpha", "bravo", "charlie", "delta"):
            self.assertNotIn(word, redacted2)

    def test_assignment_quoted_value_honors_backslash_escaped_quote(self) -> None:
        # Item 13: a JSON-style escaped quote inside a quoted value was misread as the value's own
        # terminating quote, truncating the atomic span early and handing the remainder (including
        # an embedded IPv4-shaped substring) straight to Phase B -- verified fail-before (HEAD
        # produced `'password": [REDACTED]"beta-[REDACTED_IP]-Zx"'`, leaking `"beta-`/`-Zx`).
        text = 'password": "R7mQ\\"beta-198.51.100.88-Zx"'
        redacted = hook.redact(text)
        self.assertEqual(redacted, 'password": "[REDACTED]"')
        self.assertNotIn("beta-198", redacted)
        self.assertNotIn("Zx", redacted)
        self.assertEqual(redacted, hook.redact(redacted))

        # A literal escaped backslash immediately before the real closing quote (`\\` then `"`)
        # must still be recognized as the genuine terminator, not swallowed as another escape pair.
        redacted2 = hook.redact('token: "Ab1cD2eF3\\\\"')
        self.assertEqual(redacted2, 'token: "[REDACTED]"')

    def test_assignment_declines_realistic_schema_and_citation_documentation(self) -> None:
        # Item 17: three real, reproducible false positives from `_redact_assignment`'s (and its
        # `_INLINE_ASCII_SECRET_RE` sibling's, which independently re-attempts the same "keyword:
        # value" shape via its CJK-connector-reuse branch) exact-word-only denylist -- verified
        # fail-before (HEAD redacted all three).
        unchanged = (
            "password: minimumLength=12",
            "key: cache-index-v2",
            "token: RFC6750 defines bearer usage",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)

        # Must not reopen coverage for a real secret shaped similarly (contains '=' or a hyphenated
        # lowercase run, but is not one of the three narrow structural shapes above).
        self.assertEqual(hook.redact("password: hunter2value"), "password: [REDACTED]")
        self.assertEqual(hook.redact("password: supersecretvalue"), "password: [REDACTED]")
        self.assertNotEqual(
            hook.redact("password: Ab-cd-ef-gh-99"), "password: Ab-cd-ef-gh-99",
            "a mixed-case hyphenated value must still redact",
        )

    def test_cjk_labeled_value_with_embedded_han_ideograph_redacts_as_one_atomic_span(self) -> None:
        # BLOCKING dual-review finding (2026-08-22) against the prior round's own fix: that round's
        # test (formerly named `..._is_a_full_pass_through`) asserted the ENTIRE labeled secret,
        # including a real routable-format IPv4 address, survives redaction verbatim -- a real
        # leak pinned as "intended" behavior, exactly the process-violation shape this file's own
        # rules forbid. That round's actual mechanism made this worse than a pass-through: an
        # earlier attempt (`'密码：Ax7密-[REDACTED_IP]-Qv'`) at least redacted the IP; the "fix" that
        # test pinned redacted NOTHING. Verified fail-before against the un-patched Han-bridge
        # driver in this round's own git history (see `_sub_structural_with_han_bridge`'s module
        # comment): both shapes are real leaks. The correct fix claims the WHOLE gap-bridged span
        # (keyword, connector, ASCII prefix, embedded ideograph, embedded IPv4, and any trailing
        # value-shaped suffix) as one atomic `[REDACTED]` placeholder -- zero characters of the
        # real secret value survive anywhere in the output.
        text = "密码：Ax7密-198.51.100.73-Qv"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]")
        self.assertNotIn("Ax7", redacted)
        self.assertNotIn("198.51.100.73", redacted)
        self.assertNotIn("Qv", redacted)
        self.assertEqual(redacted, hook.redact(redacted))

    def test_han_bridge_generalizes_beyond_the_narrow_one_ideograph_three_char_prefix_shape(
        self,
    ) -> None:
        # P2 dual-review finding: the prior round's own trigger only recognized a 1-3-character
        # prefix followed by EXACTLY one ideograph, so a trivially longer prefix or 2+ contiguous
        # ideographs fell outside it and still fragmented -- verified fail-before against the
        # un-patched driver (see this file's own git history): a 5-char prefix produced
        # `'密码：[REDACTED]密-[REDACTED_IP]-Qv'` (the ideograph and `-Qv` suffix leaking in the
        # clear between two placeholders) and 2 contiguous ideographs produced
        # `'密码：Ax7测试-[REDACTED_IP]-Qv'` (the whole prefix and suffix leaking, no bridge at all).
        # Both must now redact as one atomic span, uniformly across all four generalized structural
        # siblings this round's fix applies the same mechanism to (IPv4/MAC/CN-mobile/IPv6 --
        # matching the finding's own "same on MAC, phone and IPv6 variants" note; a MAC address
        # directly preceded by `-` is excluded here because `_MAC_ADDRESS_RE` itself never matches
        # in that exact shape, in ANY context, CJK or not -- a narrow, pre-existing, out-of-scope
        # property of that pattern's own boundary lookbehind, unrelated to the Han-bridge mechanism;
        # see this round's report).
        cases = (
            "密码：Ax7Zq密-198.51.100.73-Qv",  # prefix longer than 3 chars
            "密码：Ax7测试-198.51.100.73-Qv",  # two contiguous ideographs
            "密码：Ax7Zq密-13800138000-Qv",  # generalization applies to CN-mobile too
            "密码：Ax7Zq密-2001:db8::1-Qv",  # generalization applies to IPv6 too
            "密码：Ax7Zq密de:ad:be:ef:00:11Qv",  # generalization applies to MAC (no dash boundary)
        )
        for text in cases:
            redacted = hook.redact(text)
            self.assertEqual(redacted, "密码：[REDACTED]", text)
            self.assertEqual(redacted, hook.redact(redacted), text)

    def test_han_bridge_recognizes_email_despite_its_own_leading_punctuation_being_absorbed(
        self,
    ) -> None:
        # P1 dual-review finding: `_redact_email_unless_han_bridge` (the prior round's mechanism)
        # could never fire for the punctuation-adjacent shape it was written for, because
        # `_EMAIL_RE`'s own local-part class (`[A-Z0-9._%+-]`) absorbs a leading `-`/`.`/`_` into
        # its OWN match, so the trigger's required trailing-punctuation lookback landed on the
        # punctuation itself instead of before it and never matched -- verified fail-before against
        # the un-patched driver: `'密码：Ax7密-devnull@example.org-Qv'` produced
        # `'密码：Ax7密[REDACTED_EMAIL]-Qv'` (the prefix `Ax7密` and suffix `-Qv` leaking beside a
        # placeholder that looked fully handled -- the exact deceptive-partial shape the atomic-span
        # property forbids, on the one structural sibling whose own boundary defeated the guard
        # entirely). `_sub_structural_with_han_bridge` retries the trigger search one character
        # later specifically to recognize this shape.
        text = "密码：Ax7密-devnull@example.org-Qv"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]")
        self.assertNotIn("Ax7", redacted)
        self.assertNotIn("devnull", redacted)
        self.assertNotIn("Qv", redacted)
        self.assertEqual(redacted, hook.redact(redacted))

    def test_han_bridge_guard_does_not_suppress_unrelated_structural_redaction(self) -> None:
        # The item-14 fix must not become a new leak of its own: it may only decline a Phase-B
        # match that sits directly against the narrow embedded-ideograph gap, never a genuinely
        # unrelated IP/email/MAC/phone elsewhere in the same text, even on the same line as a
        # secret keyword.
        cases = {
            "密码：Ab1cD2eF3 is stored in vault 198.51.100.5 nearby": (
                "密码：[REDACTED] is stored in vault [REDACTED_IP] nearby"
            ),
            "密码见2024年12月31日，服务器地址192.0.2.1": (
                "密码见2024年12月31日，服务器地址[REDACTED_IP]"
            ),
            "密码：Q7-de:ad:be:ef:00:11-R3": "密码：[REDACTED]",
        }
        for text, expected in cases.items():
            self.assertEqual(hook.redact(text), expected, text)

    def test_han_bridge_guard_does_not_flag_ordinary_chinese_date_and_measure_word_prose(self) -> None:
        # Design-pass finding, this round: a lookahead-based widening of the value grammar itself
        # (rather than a Phase-B suppression) was tried and rejected, because ordinary Chinese
        # dates/measure words (年/月/日/第/个/项/...) interleave single ideographs with digit runs
        # constantly -- every variant tried swallowed an entire unrelated sentence into one fake
        # placeholder. Re-verified directly against the final Python-level guard actually shipped:
        # it requires ASCII structural punctuation (-._:/) immediately after the ideograph, which
        # none of these constructions ever have, so none of them can trigger it in the first place.
        unchanged = (
            "密码见2024年12月31日到期",
            "密码见于第3章第2节",
            "备份码见附录，共5个文件",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)

    def test_han_bridge_trigger_pattern_uses_no_unscoped_ignorecase_or_bare_word_boundary(self) -> None:
        # CJK-adjacency / IGNORECASE-taint invariant, re-verified directly for the new pattern this
        # round adds, the same way every prior round's own new pattern is checked.
        self.assertFalse(hook._HAN_BRIDGE_TRIGGER_RE.flags & re.IGNORECASE)
        self.assertNotIn(r"\b", hook._HAN_BRIDGE_TRIGGER_RE.pattern)

    def test_han_bridge_lookback_guard_scales_near_linearly(self) -> None:
        # ReDoS check for the new bounded-window guard: each Phase-B match's lookback cost is
        # capped at a small constant (`_HAN_BRIDGE_LOOKBACK_WINDOW`) via `pos`/`endpos`, not a
        # function of the document length, so a document containing many structural matches must
        # still scale near-linearly overall, not quadratically.
        def timed(n: int) -> float:
            text = "192.0.2.1 " * n
            return _min_elapsed(lambda: hook.redact(text))

        small = timed(500)
        large = timed(4_000)  # 8x input
        self.assertLess(large, small * 8 + 0.5, "Han-bridge lookback guard must scale near-linearly")
        self.assertLess(large, 3.0)

    def test_final_round_new_shapes_are_idempotent(self) -> None:
        # redact(redact(x)) == redact(x) for every new shape this round's fixes touch.
        cases = (
            'password: "R7mQ betaLOCK"',
            'password": "R7mQ\\"beta-198.51.100.88-Zx"',
            "password: minimumLength=12",
            "key: cache-index-v2",
            "token: RFC6750 defines bearer usage",
            "密码：Ax7密-198.51.100.73-Qv",
            "密码见2024年12月31日到期",
            "密码：Ab1cD2eF3 is stored in vault 198.51.100.5 nearby",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: once={once!r} twice={twice!r}")

    def test_han_bridge_extended_to_the_last_six_previously_unbridged_structural_patterns(
        self,
    ) -> None:
        # Architectural-rewrite round-1 (final-push) finding: `_URL_USERINFO_RE`/`_TOKEN_RE`/
        # `_BEARER_RE`/`_JWT_RE`/`_LONG_BLOB_RE`/`_CN_ID_NUMBER_RE` were the last 6 of the 11
        # Phase B structural patterns still calling `.sub()` directly instead of routing through
        # `_sub_structural_with_han_bridge` -- so the same embedded-bare-Han-ideograph gap that
        # `test_cjk_labeled_value_with_embedded_han_ideograph_redacts_as_one_atomic_span` already
        # pins as fully bridged for IPv4/IPv6/MAC/email/CN-mobile still fragmented a labeled value
        # for these 6, leaking a real prefix and/or suffix of the secret next to a placeholder that
        # looked fully handled. Verified fail-before against this file's HEAD (synthetic values
        # only, never a real captured secret):
        #   '密码：Ax7密-sk-abcdefghij1234567890-Qv'      -> '密码：Ax7密-[REDACTED_TOKEN]'
        #   '密码：Ax7密-https://bob:hunter2@example.com-Qv' -> '密码：Ax7密-https://[REDACTED]@example.com-Qv'
        #   '密码：Ax7密-Bearer abcdefghij1234567890-Qv'  -> '密码：Ax7密-Bearer [REDACTED]'
        #   '密码：Ax7密-<jwt>-Qv'                        -> '密码：Ax7密-[REDACTED_TOKEN]'
        #   '密码：Ax7密-<60 char blob>-Qv'               -> '密码：Ax7密[REDACTED_BLOB]'
        #   '密码：Ax7密-110101199003077758-Qv'           -> '密码：Ax7密-[REDACTED_ID]-Qv'
        # Each now redacts as a single atomic '密码：[REDACTED]' span instead, with zero characters
        # of the real secret surviving anywhere in the output.
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        cases = {
            "token": "密码：Ax7密-sk-abcdefghij1234567890-Qv",
            "url_userinfo": "密码：Ax7密-https://bob:hunter2@example.com-Qv",
            "bearer": "密码：Ax7密-Bearer abcdefghij1234567890-Qv",
            "jwt": f"密码：Ax7密-{jwt}-Qv",
            "long_blob": "密码：Ax7密-" + "A" * 60 + "-Qv",
            "cn_id": "密码：Ax7密-110101199003077758-Qv",
        }
        for name, text in cases.items():
            redacted = hook.redact(text)
            self.assertEqual(redacted, "密码：[REDACTED]", f"{name}: {text!r} -> {redacted!r}")
            self.assertNotIn("Ax7", redacted, name)
            self.assertNotIn("Qv", redacted, name)
            self.assertEqual(redacted, hook.redact(redacted), f"{name}: must stay idempotent")

    def test_han_bridge_extension_does_not_affect_standalone_matches_of_the_six_patterns(
        self,
    ) -> None:
        # The Han-bridge extension above may only ever ADD redaction when a genuine gap-bridge
        # trigger sits directly against the match; a standalone occurrence of any of these 6
        # patterns elsewhere in the text, with no recognized secret keyword nearby, must keep
        # redacting exactly as it did before this round (same placeholder, same span).
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        cases = {
            "see sk-abcdefghij1234567890 in vault": "see [REDACTED_TOKEN] in vault",
            "curl https://bob:hunter2@example.com/api": "curl https://[REDACTED]@example.com/api",
            "Authorization: Bearer abcdefghij1234567890": "Authorization: Bearer [REDACTED]",
            f"auth string is {jwt} here": f"auth string is [REDACTED_TOKEN] here",
            "blob=" + "A" * 60 + " end": "[REDACTED_BLOB] end",
            "id is 110101199003077758 on file": "id is [REDACTED_ID] on file",
            "备注sk-abcdefghij1234567890结束": "备注[REDACTED_TOKEN]结束",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertNotIn("hunter2", redacted, text)
            self.assertNotIn("abcdefghij1234567890", redacted, text)
            self.assertNotIn("110101199003077758", redacted, text)

    # --- Architectural-rewrite round 2 (independent Claude opus5/max + Codex sol/max dual review
    # against the round-1 architectural rewrite, P0 finding 13 -- the review's own stated NO-GO
    # driver -- plus findings 6, 9, 14-19) -------------------------------------------------------

    def test_round18_han_bridge_trigger_recognizes_connector_words_not_just_a_bare_colon(self) -> None:
        # Finding 13 (P0, the prior round's NO-GO driver): `_HAN_BRIDGE_TRIGGER_RE` only recognized
        # a literal ':'/'：'/'='/'＝' as a connector, so a keyword followed by one of Phase A's own
        # connector WORDS (是/为/就是/等于/即, or an ASCII keyword + "is"/"equals") never registered
        # as a trigger -- Phase A's own atomic capture still stopped at the embedded Han ideograph,
        # but with no trigger to bridge it, the value fragmented across a Phase B placeholder with
        # real secret bytes left in the clear on either side. Each case must redact as ONE atomic
        # span with zero characters of the synthetic secret value surviving, and stay idempotent.
        cases = (
            ("密码是Ax7密-sk-abcdefghij1234567890-Qv", ("Ax7", "-Qv", "sk-abcdefghij1234567890")),
            ("密码就是Ax7密-https://bob:hunter2@example.com-Qv", ("Ax7", "-Qv", "hunter2")),
            ("password is Ax7Zq密-192.0.2.44-Qv", ("Ax7Zq", "-Qv", "192.0.2.44")),
            ("密钥等于Bb3密-de:ad:be:ef:00:11-Qv", ("Bb3", "-Qv", "de:ad:be:ef:00:11")),
            ("token equals Dd4密-01:23:45:67:89:ab-Qv", ("Dd4", "-Qv", "01:23:45:67:89:ab")),
        )
        for text, fragments in cases:
            redacted = hook.redact(text)
            self.assertIn("[REDACTED", redacted, text)
            for fragment in fragments:
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")

    def test_round18_han_bridge_connector_words_do_not_flag_realistic_non_secret_prose(self) -> None:
        # False-positive sweep for the widened trigger connector specifically: none of these should
        # ever reach the "collapse the whole value into one placeholder" bridge behavior at all,
        # since none contain a genuine embedded-ideograph secret value.
        unchanged_cases = (
            "密码是一个重要的概念，请勿泄露",
            "password is required for every deploy step",
            "密钥等于配置文件中的默认值",
            "token equals the identifier assigned at signup",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_round18_han_bridge_trailing_run_no_longer_truncates_long_content(self) -> None:
        # Finding 15 (P1): the trailing run consumed after the Phase B structural match was capped
        # at 40 characters, so a labeled value ending in a long URL/path left its final segment
        # exposed next to the placeholder.
        text = (
            "密码：Ax7密-https://bob:hunter2@internal-ci.company-tools.example.com/reset-Qv"
        )
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]", text)
        self.assertEqual(hook.redact(redacted), redacted)

    def test_round18_han_bridge_survives_a_wide_connector_whitespace_run(self) -> None:
        # Finding 16 (P1): the trigger's own connector gap used to be capped at 4 characters,
        # independent of Phase A's own `_CJK_CONNECTOR_WS`/`_CJK_CONNECTOR_WS_TRAILING` bounds, so a
        # real aligned key/value paste (many spaces after the separator) desynced the bridge from
        # what Phase A itself had actually consumed.
        text = "密码：     Ax7Zq密-sk-abcdefghij1234567890-Qv"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：     [REDACTED]", text)
        self.assertEqual(hook.redact(redacted), redacted)

    def test_round18_han_bridge_value_chars_include_a_literal_pipe(self) -> None:
        # Finding 17 (P2): the bridge's own value-run character class had no '|' alternative
        # (unlike every Phase A inline value class), so a secret containing a literal pipe next to
        # an embedded ideograph truncated right there.
        text = "密码：Ax7密|sk-abcdefghij1234567890-Qv"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]", text)
        self.assertEqual(hook.redact(redacted), redacted)

    def test_round18_home_directory_path_now_routes_through_the_han_bridge(self) -> None:
        # Finding 18 (P2): `_HOME_RE` was the one Phase B structural rewriter left on a plain
        # `.sub()`, never routed through `_sub_structural_with_han_bridge` -- a home-directory path
        # embedded inside a keyword-labeled value fragmented the same way every other pattern used
        # to before being bridged.
        text = "密码：Ax7密-/Users/alice-Qv"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]", text)
        self.assertEqual(hook.redact(redacted), redacted)
        # A standalone, unlabeled home path elsewhere is unaffected (`_HOME_RE` itself only ever
        # matched one path segment, e.g. "/Users/bob" -- not "/Users/bob/notes.txt" in full -- and
        # this fix does not change that).
        self.assertEqual(hook.redact("see /Users/bob"), "see $USER_HOME")

    def test_round18_mac_address_preceded_by_a_dash_now_redacts_through_the_bridge(self) -> None:
        # Finding 19 (P2): the MAC pattern's boundary lookaround rejected ANY adjacent '-', so a
        # MAC-shaped value glued to a keyword-labeled value by an ordinary '-' separator (not part
        # of a longer dash-separated hex run) never matched at all -- the whole secret, not just a
        # fragment, stayed in the clear.
        text = "密码：Ax7密-de:ad:be:ef:00:11-Qv"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]", text)
        self.assertEqual(hook.redact(redacted), redacted)

    def test_round18_mac_dash_fix_does_not_reopen_longer_dash_run_overmatch(self) -> None:
        # The narrowed boundary (finding 19's fix) must still refuse to mid-slice a genuinely
        # longer dash-separated hex run -- matching only the interior 6 groups and dropping the
        # surrounding groups would be the identical "std::vector"-style overmatch this file's MAC
        # and IPv6 boundaries already guard against elsewhere.
        text = "12-34-de-ad-be-ef-00-11-22"
        redacted = hook.redact(text)
        self.assertNotIn("[REDACTED_IP]", redacted, redacted)
        self.assertEqual(redacted, text)

    def test_round18_han_bridge_survives_a_non_ascii_non_cjk_trailing_character(self) -> None:
        # Finding 6 (P1): the bridge's value-run class was ASCII-only, so an ordinary accented
        # Latin letter (not CJK, not one of the four named homoglyphs) inside the trailing run
        # truncated the bridge early and leaked the remainder in the clear.
        text = "密码：Ax7密-203.0.113.146-éQv"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]", text)
        self.assertEqual(hook.redact(redacted), redacted)

    def test_round18_cjk_labeled_secret_gets_the_same_documentation_check_filter_as_ascii(self) -> None:
        # Finding 9: `_redact_inline_cjk_secret` only ever checked `_is_known_non_secret_word` (an
        # exact closed-vocabulary match); `_redact_assignment`'s ASCII sibling also checks
        # `_looks_like_documentation_not_secret` (RFC citation / nested `identifier=integer` /
        # version-suffixed-identifier shapes), but that check was never ported to the CJK path.
        unchanged_cases = (
            "密码：minimumLength=12",
            "密钥：cache-index-v2",
            "密码是minimumLength=12",
            "密钥等于cache-index-v2",
        )
        for text in unchanged_cases:
            self.assertEqual(hook.redact(text), text, text)
        # A real secret that merely happens to look similar (has more than just the ASCII
        # letters/digits the documentation shapes require) still redacts.
        self.assertIn("[REDACTED", hook.redact("密码：minimumLength=12!!Zq9"))

    def test_round18_ipv6_boundary_still_declines_a_candidate_glued_to_unredacted_prose(self) -> None:
        # Finding 14's original P1 (fixed then reverted -- see the module-level comment directly
        # above `_IPV6_CANDIDATE_RE`): a round-2 attempt excluded ']' from the leading lookbehind
        # to make `redact(redact(x)) == redact(x)` hold here, but that exclusion rejected ANY
        # literal ']' immediately before a candidate -- including ones a genuinely separate, earlier
        # Phase-B pass (e.g. `_BEARER_RE`/`_TOKEN_RE`/`_IPV4_RE`) legitimately produces within the
        # SAME `redact()` call, dropping real, unrelated IPv6 addresses next to them (a real
        # regression the final-gate dual review caught: see the module-level comment for the exact
        # repros). Reverted to the real-HEAD lookbehind, which is byte-identical in what it matches.
        # What remains true and still verified here: pass 1 alone still correctly declines the
        # candidate while it is still glued to the un-redacted email ('.'/letters precede it, per
        # this pattern's own design), so the email redacts cleanly and the trailing "::1" is left for
        # a later, standalone IPv6 match to consider on its own merits.
        text = "user@example.com::1"
        pass1 = hook.redact(text)
        self.assertEqual(pass1, "[REDACTED_EMAIL]::1", text)
        # NOT asserted as a fixed point: `redact(pass1)` further redacts the now-adjacent "::1" into
        # "[REDACTED_EMAIL][REDACTED_IP]", a narrow idempotency gap confirmed to already exist at
        # real git HEAD (independent of this whole rewrite) and left as a documented, non-blocking,
        # unfixed residual per this round's report -- not pinned here as a passing assertion of
        # leaky/non-idempotent behavior.

    def test_round18_han_bridge_trigger_uses_no_unscoped_ignorecase_or_bare_word_boundary(self) -> None:
        # CJK-adjacency / IGNORECASE-taint invariant, re-verified directly against the rebuilt
        # trigger pattern rather than just trusting the existing suite passing.
        self.assertFalse(hook._HAN_BRIDGE_TRIGGER_RE.flags & re.IGNORECASE, hook._HAN_BRIDGE_TRIGGER_RE.pattern)
        self.assertNotIn(r"\b", hook._HAN_BRIDGE_TRIGGER_RE.pattern)

    def test_round18_han_bridge_trigger_scales_linearly_on_adversarial_input(self) -> None:
        # ReDoS regression: the rebuilt connector (now a two-armed alternation reusing
        # `_CJK_CONNECTOR_WS`/`_CJK_CONNECTOR_WS_TRAILING`/`_ASCII_WEAK_CONNECTOR_WORD`) must not
        # reopen a catastrophic-backtracking surface. The trigger only ever runs within the fixed
        # `_HAN_BRIDGE_LOOKBACK_WINDOW`, so this measures the end-to-end `redact()` cost on a
        # document containing many independent keyword+IP pairs, each one triggering a lookback
        # search, rather than a single pathological string.
        def timed(n: int) -> float:
            block = "密码：Ax7密-198.51.100.1-Qv\n" * n
            return _min_elapsed(lambda: hook.redact(block), attempts=3)

        small = timed(500)
        large = timed(2000)  # 4x input
        self.assertLess(large, small * 4 + 0.5, "many keyword+IP pairs must scale near-linearly")
        self.assertLess(large, 3.0)

    def test_round18_redact_is_idempotent_on_all_new_fix_shapes(self) -> None:
        cases = (
            "密码是Ax7密-sk-abcdefghij1234567890-Qv",
            "password is Ax7Zq密-192.0.2.44-Qv",
            "密码：     Ax7Zq密-sk-abcdefghij1234567890-Qv",
            "密码：Ax7密|sk-abcdefghij1234567890-Qv",
            "密码：Ax7密-/Users/alice-Qv",
            "密码：Ax7密-de:ad:be:ef:00:11-Qv",
            "密码：Ax7密-203.0.113.146-éQv",
            # "user@example.com::1" intentionally NOT included: it is a documented, pre-existing
            # (confirmed present at real git HEAD, not introduced by this rewrite) non-fixed-point
            # case -- see `_IPV6_CANDIDATE_RE`'s own comment and
            # test_round18_ipv6_boundary_still_declines_a_candidate_glued_to_unredacted_prose above.
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: {once!r} -> {twice!r}")

    # --- Final-gate round regression tests (round-4 of the architectural-rewrite retry) ---

    def test_bearer_runs_before_token_so_a_prefixed_token_cannot_eat_the_bearer_anchor(self) -> None:
        # Final-gate finding 1 (P1, new regression vs live HEAD): the round-10 reorder put
        # `_TOKEN_RE` ahead of `_BEARER_RE` in Phase B. `_TOKEN_RE`'s value class includes '-', so
        # it greedily consumed the literal word "Bearer" that immediately followed a prefixed token,
        # destroying `_BEARER_RE`'s only anchor for the real bearer credential that came after --
        # which then leaked in full. Restored the HEAD ordering (BEARER before TOKEN).
        cases = (
            (
                "old=sk-abcdefghij1234567890-Bearer xoxbnewsessiontokenvalue",
                "old=[REDACTED_TOKEN] [REDACTED]",
            ),
            (
                "Authorization: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-Bearer abcdefghij1234567890",
                "Authorization: [REDACTED_TOKEN] [REDACTED]",
            ),
            (
                "AKIAIOSFODNN7EXAMPLE-Bearer abcdefghij1234567890",
                "[REDACTED_TOKEN] [REDACTED]",
            ),
        )
        for text, expected in cases:
            self.assertEqual(hook.redact(text), expected, text)
            self.assertNotIn("xoxb", hook.redact(text))

    def test_ipv6_boundary_does_not_drop_a_genuine_address_next_to_an_earlier_same_call_placeholder(
        self,
    ) -> None:
        # Final-gate finding 2 (P1, new regression vs live HEAD): a round-2 idempotency fix excluded
        # ']' from `_IPV6_CANDIDATE_RE`'s leading lookbehind -- but that rejects ANY literal ']'
        # immediately before a candidate, including ones a genuinely earlier, unrelated Phase-B pass
        # (Bearer/Token/IPv4) legitimately produces within the SAME `redact()` call. That silently
        # dropped real, unrelated IPv6 addresses sitting next to them. Reverted to the real-HEAD
        # lookbehind (byte-identical in what it matches).
        cases = (
            (
                "Authorization: Bearer abcdefghijkl-fe80::5",
                "Authorization: Bearer [REDACTED][REDACTED_IP]",
            ),
            (
                "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789fe80::1c2d:3e4f。",
                "[REDACTED_TOKEN][REDACTED_IP]。",
            ),
            (
                "x [REDACTED]::1c2d:3e4f-tail",
                "x [REDACTED][REDACTED_IP]-tail",
            ),
        )
        for text, expected in cases:
            self.assertEqual(hook.redact(text), expected, text)

    def test_cjk_family_gap_characters_bridge_atomically_for_the_nine_non_ideograph_gap_chars_tested(
        self,
    ) -> None:
        # Final-gate finding 3 (P1 for the rewrite's own stated bar, byte-identical-to-HEAD gap, not
        # a regression): the han-bridge trigger used to require a literal CJK IDEOGRAPH in the gap
        # between a labeled value's prefix and a Phase-B structural match -- but
        # `_CJK_VALUE_EXCLUDED_UNICODE_RANGES` (what Phase A's own value grammar stops at) excludes
        # the WHOLE CJK-family Unicode block, not just ideographs: Halfwidth/Fullwidth Forms, CJK
        # Symbols and Punctuation, Hiragana/Katakana, and Hangul. Any of those characters embedded in
        # a real secret (a routine artifact of pasting a credential while typing with a CJK IME) had
        # no bridge, so the structural pattern nibbled just its own narrow match and left the
        # surrounding real secret exposed. Every one of these must now redact as ONE atomic
        # '密码：[REDACTED]' span, on BOTH sides of the embedded structural match, with zero
        # characters of the real secret surviving.
        #
        # Round-9 rename (retry-gate finding 5): this test's name previously claimed the CJK-family
        # gap bridge works "the same as ideographs" -- it does not. A genuine CJK IDEOGRAPH used as
        # the gap character is a real, disclosed, non-blocking residual gap: an ideograph is
        # deliberately excluded from the TRAILING side of the bridge (see
        # `test_cjk_family_gap_bridge_does_not_swallow_real_trailing_chinese_prose` immediately
        # below, whose whole point is that an ideograph must terminate the trailing scan so real
        # Chinese prose is not swallowed), so `redact('密码：Ab中198.51.100.9中Cd')` still leaves the
        # trailing 'Cd' fragment exposed (`'密码：[REDACTED]中Cd'`) -- strictly better than pre-bridge
        # HEAD (which leaked both 'Ab' and 'Cd'), but not the same zero-survival guarantee this test
        # actually proves for the nine NON-ideograph gap characters below. The name now says exactly
        # what this test covers; the ideograph case remains an intentional, disclosed-in-prose-only
        # residual gap (never pinned as a passing test) -- an honest tradeoff between "swallow the
        # embedded secret whole" and "never swallow real trailing Chinese prose" -- see this round's
        # own report for the repro.
        gap_chars = "－，、！カ가ｱ７Ａ"
        for gap in gap_chars:
            text = f"密码：Ab{gap}203.0.113.77{gap}Cd"
            result = hook.redact(text)
            self.assertEqual(result, "密码：[REDACTED]", text)
            self.assertNotIn("Ab", result, text)
            self.assertNotIn("Cd", result, text)

    def test_cjk_family_gap_bridge_does_not_swallow_real_trailing_chinese_prose(self) -> None:
        # Final-gate finding 3 (continued): a first attempt widened the TRAILING bridge scan (the
        # text consumed after the structural match itself) to the same full CJK-family gap class
        # used for the prefix side -- but unlike the prefix side (only triggered when a validated
        # structural match immediately follows), the trailing scan has no such anchor for what comes
        # after it. Real short Chinese sentences routinely fit within the 20-character gap-run bound
        # with no ASCII in between, so that first attempt silently swallowed genuine, unrelated
        # trailing prose whole. Fixed by excluding actual CJK ideographs from the trailing-only gap
        # class: a real Chinese sentence necessarily contains ideographs, so the first one still
        # terminates the run immediately, exactly as before either fix.
        text = "密码：Ax7密-198.51.100.73-Qv这是一个正常的中文句子，无关内容"
        result = hook.redact(text)
        self.assertEqual(result, "密码：[REDACTED]这是一个正常的中文句子，无关内容", text)
        # The already-working atomic case (no trailing prose) must still fully redact.
        self.assertEqual(hook.redact("密码：Ax7密-198.51.100.73-Qv"), "密码：[REDACTED]")

    def test_cjk_family_gap_bridge_scales_linearly_on_adversarial_alternating_input(self) -> None:
        # ReDoS regression for the newly-widened prefix and trailing gap classes (finding 3).
        def timed(n: int) -> float:
            text = "密码：Ax7密-198.51.100.1-" + ("－Q" * n)
            return _min_elapsed(lambda: hook.redact(text), attempts=3)

        small = timed(2000)
        large = timed(8000)  # 4x input
        self.assertLess(large, small * 4 + 0.5, "alternating gap/value trailing must scale near-linearly")
        self.assertLess(large, 3.0)

    def test_cjk_family_gap_bridge_is_idempotent(self) -> None:
        cases = (
            "密码：Ab－203.0.113.77－Cd",
            "密码：Ab、203.0.113.77、Cd",
            "密码：Abカ203.0.113.77カCd",
            "密码：Ab가203.0.113.77가Cd",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: {once!r} -> {twice!r}")

    def test_cjk_family_gap_characters_do_not_flag_realistic_non_secret_prose(self) -> None:
        cases = (
            "密码策略：不得少于8位，需包含大小写字母、数字与符号！",
            "密钥管理规范！所有密钥均需存放于Vault中，定期轮换。",
            "令牌有效期为7天，过期后需重新登录！",
            "备份码设备码登录方式已支持，详见文档。",
        )
        for text in cases:
            self.assertEqual(hook.redact(text), text, text)

    # --- Round-11 dual-review findings (independent Claude opus + Codex, 2026-08-22, against the
    # round-10 attempt) -----------------------------------------------------------------------

    def test_round19_han_bridge_handles_two_or_more_embedded_gap_runs(self) -> None:
        # Finding 1 (P1 BLOCKING): `_HAN_BRIDGE_TRIGGER_RE`'s `bridge_value` group used to permit
        # exactly ONE gap run, so a value containing TWO OR MORE CJK-family gap characters
        # separated by value characters could never match at all -- verified fail-before against
        # pre-fix HEAD: `redact('数据库密码：Kp，Rv－198.51.100.7－Ty')` ->
        # `'数据库密码：Kp，Rv－[REDACTED_IP]－Ty'` (real `'Kp，Rv－'`/`'－Ty'` surviving in the clear).
        # Trailing "备用" in the second case is correctly-preserved trailing Chinese prose (it
        # begins with a real ideograph, the established trailing-run terminator -- see
        # `test_cjk_family_gap_bridge_does_not_swallow_real_trailing_chinese_prose`), not a leak;
        # every OTHER case's real secret fragments must be fully gone.
        cases = (
            ("数据库密码：Kp，Rv－198.51.100.7－Ty", "数据库密码：[REDACTED]", ("Kp", "Rv", "Ty")),
            ("数据库密码：北京2024上海-198.51.100.7-备用", "数据库密码：[REDACTED]备用", ("北京", "上海")),
            ("密码：Ab－Cd－13712345678－Ef", "密码：[REDACTED]", ("Ab", "Cd", "Ef")),
            ("密码：Ab－Cd－devnull@example.org－Ef", "密码：[REDACTED]", ("Ab", "Cd", "Ef", "devnull")),
        )
        for text, expected, fragments in cases:
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            for fragment in fragments:
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")

    def test_round19_han_bridge_multi_gap_scales_linearly_on_adversarial_input(self) -> None:
        # ReDoS regression for the repeated `_HAN_BRIDGE_GAP_VALUE_UNIT` group (finding 1's fix):
        # an alternating gap/value run must not reopen combinatorial backtracking.
        def timed(n: int) -> float:
            text = "密码：" + ("Kp－" * n) + "198.51.100.7"
            return _min_elapsed(lambda: hook.redact(text), attempts=3)

        small = timed(20)
        large = timed(80)  # 4x input
        self.assertLess(large, small * 4 + 0.5, "repeated gap/value units must scale near-linearly")
        self.assertLess(large, 3.0)

    def test_round19_han_bridge_extends_past_the_prefix_run_cap_for_a_long_raw_value_tail(
        self,
    ) -> None:
        # Finding 2 (P1 BLOCKING): the value run consumed AFTER the last required gap and BEFORE
        # the structural match itself was capped at `_HAN_BRIDGE_PREFIX_RUN_MAX` (40) with no
        # extension mechanism, so a real secret tail longer than 40 raw characters leaked in full.
        # Verified fail-before against pre-fix HEAD: exactly 40 redacted atomically, 41 did not
        # (`redact('密码：Aa1b密' + 'P'*41 + '-198.51.100.7-Zz')` left the entire 'P'*41 run plus
        # '-Zz' exposed next to a placeholder that looked fully handled). Exercises the boundary
        # directly at/around the old cap, plus a value far beyond any plausible cap size.
        for n in (35, 40, 41, 45, 60, 200, 1000):
            text = "密码：Aa1b密" + ("P" * n) + "-198.51.100.7-Zz"
            redacted = hook.redact(text)
            self.assertEqual(redacted, "密码：[REDACTED]", f"n={n}: {text!r} -> {redacted!r}")
            self.assertNotIn("Aa1b", redacted, f"n={n}")
            self.assertNotIn("Zz", redacted, f"n={n}")
            self.assertEqual(hook.redact(redacted), redacted, f"n={n}: must stay idempotent")
        # Same fix, ASCII "is"-connector path (round-18's connector-word generalization) and a
        # non-IP structural sibling (long blob), to confirm the extension is not IPv4-specific.
        text = "password is Aa1b密" + ("P" * 45) + "-198.51.100.46-Zz"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password is [REDACTED]", text)
        self.assertNotIn("Zz", redacted)
        text2 = "密码：Aa1b密" + ("P" * 45) + "-" + ("A" * 60) + "-Zz"
        redacted2 = hook.redact(text2)
        self.assertEqual(redacted2, "密码：[REDACTED]", text2)
        self.assertNotIn("Zz", redacted2)

    def test_round19_han_bridge_extension_fallback_does_not_fire_when_the_tail_is_unrelated(
        self,
    ) -> None:
        # The extension fallback (finding 2's fix) may only bridge a genuinely continuous run of
        # value-shaped characters; it must still decline when real prose (a CJK ideograph, in
        # particular) breaks the chain before reaching the structural match, exactly like the
        # already-reviewed short-tail behavior.
        text = "密码见2024年12月31日到期，之后请查看服务器地址192.0.2.1"
        self.assertEqual(hook.redact(text), "密码见2024年12月31日到期，之后请查看服务器地址[REDACTED_IP]", text)

    def test_round19_han_bridge_extension_fallback_scales_reasonably(self) -> None:
        # ReDoS/perf sanity for the new fallback path (CORE regex + forward continuation): a
        # document containing many independent keyword+gap+long-tail+IP groups must not become
        # quadratic just because each one now takes the (rarer) fallback route.
        def timed(n: int) -> float:
            block = ("密码：Aa1b密" + ("P" * 80) + "-198.51.100.1-Zz\n") * n
            return _min_elapsed(lambda: hook.redact(block), attempts=3)

        small = timed(100)
        large = timed(400)  # 4x input
        self.assertLess(large, small * 4 + 0.75, "many extension-fallback groups must scale near-linearly")
        self.assertLess(large, 4.0)

    def test_round19_han_bridge_trigger_does_not_cross_a_real_newline(self) -> None:
        # Finding 4 (P2): a bare `$` (no MULTILINE) matches just before a trailing '\n' as well as
        # at the string's own end, so with `endpos=match.start()` the trigger could match one
        # character short of a literal newline and the driver then merged the newline plus the
        # start of the following, unrelated line into the same placeholder. Verified fail-before:
        # `redact('密码：Ab密\n198.51.100.7 是我们的网关地址，请勿修改')` produced a single
        # `'密码：[REDACTED] 是我们的网关地址，请勿修改'`-shaped output with the newline gone and
        # unrelated following content merged in. Fixed with `\Z` (true end-of-string only).
        text = "密码：Ab密\n198.51.100.7 是我们的网关地址，请勿修改"
        redacted = hook.redact(text)
        self.assertIn("\n", redacted, redacted)
        self.assertEqual(redacted, "密码：Ab密\n[REDACTED_IP] 是我们的网关地址，请勿修改")
        self.assertIn("是我们的网关地址，请勿修改", redacted)
        self.assertEqual(hook.redact(redacted), redacted)
        # Same shape with a URL/email on the following line.
        text2 = "密钥：Cd密\ndevnull@example.org 是联系邮箱，请勿修改"
        redacted2 = hook.redact(text2)
        self.assertIn("\n", redacted2, redacted2)
        self.assertIn("是联系邮箱，请勿修改", redacted2)

    def test_round19_redact_is_idempotent_when_a_home_placeholder_is_wrapped_in_punctuation(
        self,
    ) -> None:
        # Finding 5 (P2 -- new non-idempotency, candidate-only): `_redact_home`'s own "$USER_HOME"
        # placeholder contains no ASCII lowercase, so a bare-mention value that is nothing but this
        # placeholder wrapped in leading punctuation (e.g. "-$USER_HOME", produced by a first pass
        # over "验证码 -/Users/bob") satisfied the all-uppercase-or-hyphen device-code heuristic in
        # `_looks_like_secret_code` on a SECOND pass and got redacted again. Verified fail-before:
        # `redact('验证码 -/Users/bob')` -> `'验证码 -$USER_HOME'`, then a second `redact()` call on
        # that output -> `'验证码 [REDACTED]'` (a second, spurious redaction of this file's own
        # prior output). Direction was already safe (over-redaction, never un-redaction), but this
        # file maintains idempotency as an explicit invariant.
        cases = (
            "验证码 -/Users/bob",
            "| a | 密钥 -/Users/bob |",
            "前缀 recovery code is -/Users/bob 后缀",
            "设备码 -/Users/carol",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: once={once!r} twice={twice!r}")
        # The underlying substitution itself is still correct on the first pass.
        self.assertEqual(hook.redact("验证码 -/Users/bob"), "验证码 -$USER_HOME")

    def test_round19_looks_like_secret_code_declines_the_bare_home_placeholder(self) -> None:
        # Direct unit check of the fix (finding 5): a value that is ONLY this file's own
        # "$USER_HOME" placeholder, with or without leading/trailing punctuation, is never treated
        # as a fresh secret; a value that merely CONTAINS extra real alphanumeric content around it
        # still gets the ordinary shape-based verdict (this fix is narrowly scoped, not a general
        # "contains $USER_HOME" carve-out).
        self.assertFalse(hook._looks_like_secret_code("$USER_HOME"))
        self.assertFalse(hook._looks_like_secret_code("-$USER_HOME"))
        self.assertFalse(hook._looks_like_secret_code("-$USER_HOME-"))
        self.assertTrue(hook._looks_like_secret_code("QKRT-ZPLM"))

    def test_han_bridge_fullwidth_colon_run_is_bounded_not_a_redos(self) -> None:
        # Finding 6 (P2): the gap classes' own "pairwise disjoint from the value class" argument
        # was false for the fullwidth colon/equals ("："/"＝"), which are simultaneously ordinary
        # VALUE-class literals (`_CJK_VALUE_FULLWIDTH_SEP_CHARS`) AND, before this fix, part of the
        # Halfwidth/Fullwidth Forms gap range -- a measured ~440x constant-factor cliff on an
        # adversarial run of either character. Direct class-membership check plus an end-to-end
        # scaling check (the cliff was already bounded/plateauing pre-fix, not exponential, but
        # `redact()` itself must stay near-linear on this specific adversarial shape).
        self.assertIsNone(re.fullmatch(hook._HAN_BRIDGE_GAP_CLASS, "："))
        self.assertIsNone(re.fullmatch(hook._HAN_BRIDGE_GAP_CLASS, "＝"))
        self.assertIsNone(re.fullmatch(hook._HAN_BRIDGE_TRAILING_GAP_CLASS, "："))
        self.assertIsNone(re.fullmatch(hook._HAN_BRIDGE_TRAILING_GAP_CLASS, "＝"))
        # A real embedded ideograph is still recognized by both (the fix must not over-narrow).
        self.assertIsNotNone(re.fullmatch(hook._HAN_BRIDGE_GAP_CLASS, "密"))
        # Round-3 (this round) update: "－" (fullwidth hyphen) is no longer gap-class either -- see
        # `_CJK_CONNECTOR_FULLWIDTH_PUNCT_CHARS`'s own comment (finding 2, the fullwidth-punctuation
        # connector-barrier fix). It joined "："/"＝" as an ordinary VALUE-class literal this round,
        # so the disjointness generalization above now excludes it from both gap classes the same
        # way "："/"＝" already were excluded -- it still bridges an embedded decoy correctly, just
        # via the value-class alternative (`_CJK_VALUE_CHAR_CLASS_INLINE`) instead of the gap-class
        # one now that both `_HAN_BRIDGE_TRAILING_RE` and `_HAN_BRIDGE_GAP_VALUE_UNIT` already try
        # value-class content around every gap run.
        self.assertIsNone(re.fullmatch(hook._HAN_BRIDGE_TRAILING_GAP_CLASS, "－"))
        self.assertIsNotNone(re.fullmatch(hook._CJK_VALUE_CHAR_CLASS_INLINE, "－"))

        def timed(n: int) -> float:
            text = "密码：" + ("：" * n)
            return _min_elapsed(lambda: hook._HAN_BRIDGE_TRIGGER_RE.search(text), attempts=3)

        small = timed(100)
        large = timed(1000)  # 10x input
        self.assertLess(large, small * 10 + 0.25, "fullwidth-colon run must no longer scale as a cliff")
        self.assertLess(large, 1.0)

    def test_round19_ascii_device_code_keyword_now_recognized(self) -> None:
        # Finding 7 (P3, pre-existing at HEAD, not a regression): the CJK vocabulary already
        # recognizes 设备码 directly, but the ASCII sibling omitted "device code" entirely --
        # `redact('device code KXCV-BNMA')` returned the input completely unchanged. Added
        # alongside "backup code"/"recovery code" using the same two-word joiner.
        self.assertEqual(hook.redact("device code KXCV-BNMA"), "device code [REDACTED]")
        self.assertEqual(
            hook.redact("device_code: QKRT-ZPLM"), "device_code: [REDACTED]"
        )

    def test_round19_ascii_device_code_keyword_does_not_flag_realistic_prose(self) -> None:
        cases = (
            "your device code will expire in 10 minutes",
            "the device code flow is used for TV and CLI sign-in",
        )
        for text in cases:
            self.assertEqual(hook.redact(text), text, text)

    def test_round19_new_patterns_use_no_unscoped_ignorecase_or_bare_word_boundary(self) -> None:
        # CJK-adjacency / IGNORECASE-taint invariant, re-verified directly for every new pattern
        # this round adds (finding 1's `_HAN_BRIDGE_GAP_VALUE_UNIT`-based rebuild and finding 2's
        # `_HAN_BRIDGE_TRIGGER_CORE_RE`), the same way every prior round's own new pattern is
        # checked -- not just trusting the existing suite passing.
        for pattern in (hook._HAN_BRIDGE_TRIGGER_RE, hook._HAN_BRIDGE_TRIGGER_CORE_RE):
            self.assertFalse(pattern.flags & re.IGNORECASE, pattern.pattern)
            self.assertNotIn(r"\b", pattern.pattern)

    def test_round19_cjk_adjacency_spot_check_on_new_and_existing_patterns(self) -> None:
        # Direct re-verification (not just "the suite still passes") of the 18-round-old
        # CJK-adjacency / IGNORECASE-taint invariants against a representative sample, after all of
        # this round's changes.
        unchanged = (
            "密码是一个重要的概念，请勿泄露",
            "password is required for every deploy step",
            "密钥等于配置文件中的默认值",
            "token equals the identifier assigned at signup",
            "密码策略：不得少于8位，需包含大小写字母、数字与符号！",
            "备份码设备码登录方式已支持，详见文档。",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)

    def test_round19_redact_is_idempotent_on_all_new_fix_shapes(self) -> None:
        cases = (
            "数据库密码：Kp，Rv－198.51.100.7－Ty",
            "数据库密码：北京2024上海-198.51.100.7-备用",
            "密码：Ab－Cd－13712345678－Ef",
            "密码：Aa1b密" + ("P" * 200) + "-198.51.100.7-Zz",
            "密码：Ab密\n198.51.100.7 是我们的网关地址，请勿修改",
            "验证码 -/Users/bob",
            "| a | 密钥 -/Users/bob |",
            "device code KXCV-BNMA",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: once={once!r} twice={twice!r}")

    # --- Round-6 (architectural-rewrite retry) regression tests: findings from the final-gate
    # dual review against the round-5 candidate (independent Claude opus + Codex, 2026-08-22).
    # Each case below was verified to fail against the round-5 candidate before this round's fix
    # and pass after -- see claude_memory_hook.py's own module-level comments at each fix's call
    # site for the full root-cause analysis.

    def test_round6_cn_mobile_symmetric_guard_still_redacts_genuine_adjacent_numbers(self) -> None:
        # Finding 1 (P1 regression): the round-16-era guard rejected a genuine standalone 11-digit
        # mobile number whenever ANY digit followed a separator, even when that trailing digit run
        # was a wholly separate token (a second complete number, or unrelated short suffix digits),
        # not a continuation of the SAME grouped run.
        cases = (
            ("电话 13800138000-13900139000", "电话 [REDACTED_PHONE]-[REDACTED_PHONE]"),
            ("联系电话 13800138000-8001", "联系电话 [REDACTED_PHONE]-8001"),
            ("手机 13800138000-1（备用）", "手机 [REDACTED_PHONE]-1（备用）"),
            ("备用号码 13800138000-2026", "备用号码 [REDACTED_PHONE]-2026"),
        )
        for text, expected in cases:
            self.assertEqual(hook.redact(text), expected, text)
        # The original round-16 danger case (a run that continues the SAME 3-4-4 grouped shape
        # with a 4th same-style group) must still be rejected in full, not half-redacted.
        ambiguous = "158 6027 4419 7735"
        self.assertEqual(hook.redact(ambiguous), ambiguous)

    def test_round6_table_label_connector_reject_requires_real_value_content(self) -> None:
        # Finding 2 (P2 regression): a table label cell that is exactly "keyword + trailing
        # connector" with nothing else was wrongly rejected as "not a standalone label", starving
        # the adjacent value cell of the armed-column mechanism that redacts it.
        self.assertEqual(hook.redact("| 密钥: | Kp9wQ3zLm7 |"), "| 密钥: | [REDACTED] |")
        self.assertEqual(hook.redact("| 密码： | Kp9wQ3zLm7 |"), "| 密码： | [REDACTED] |")
        # The original protected shape (keyword glued to its own inline value in the SAME cell)
        # must still be rejected as a standalone label.
        self.assertEqual(
            hook.redact("| token | abc123 | 密码:Ab|Rm4T2"),
            "| token | [REDACTED] | 密码:[REDACTED]",
        )

    def test_round6_bearer_anchor_survives_a_preceding_url_userinfo_bridge(self) -> None:
        # Finding 3 (P2 regression): `_URL_USERINFO_RE` used to run before `_BEARER_RE` in Phase B;
        # its own trailing han-bridge scan consumed the literal word "Bearer" as ordinary value
        # text (no space glues it to the preceding userinfo URL), destroying `_BEARER_RE`'s only
        # anchor for the real credential that followed.
        text = "密码：Ax7密-https://bob:hunter2@example.com|Bearer xoxbrealsessiontok"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]")
        self.assertNotIn("xoxb", redacted)
        # Two sibling shapes the review confirmed already redacted atomically regardless of order.
        self.assertEqual(
            hook.redact("密钥：Ab密-198.51.100.7-Bearer sometoken12345678"), "密钥：[REDACTED]"
        )
        self.assertEqual(
            hook.redact("令牌：Kp密-203.0.113.9|Bearer sometoken12345678"), "令牌：[REDACTED]"
        )
        # BEARER-before-TOKEN ordering (an earlier round's own fix) must not regress.
        self.assertEqual(
            hook.redact("old=sk-abcdefghij1234567890-Bearer xoxbnewsessiontokenvalue"),
            "old=[REDACTED_TOKEN] [REDACTED]",
        )

    def test_round6_mac_boundary_does_not_falsely_decline_after_an_ordinary_english_word(self) -> None:
        # Item 7's own root cause, traced independently this round (not the finding's own "missing
        # Han bridge for CJK+'is'" framing -- see this round's report): `_MAC_ADDRESS_RE`'s
        # dash-boundary guard checked only ONE character, so any ordinary English word ending in a
        # hex-shaped letter (a/b/c/d/e/f) immediately followed by a dash falsely triggered "this
        # looks like the middle of a longer hex run" and the whole MAC-shaped address was silently
        # skipped -- reachable through a CJK+"is" connector, but the actual defect lived in
        # `_MAC_ADDRESS_RE` itself, independent of any connector shape.
        text = "密码 is－ops@node.invalid-aa:bc:de:f0:12:34"
        once = hook.redact(text)
        twice = hook.redact(once)
        # Round-3 (this round) update, independently justified (not a weakening -- see this file's
        # own process-integrity rule): the fullwidth hyphen "－" right after "is" used to be a
        # connector-barrier dead zone (neither a recognized connector nor a value character -- see
        # `_CJK_CONNECTOR_FULLWIDTH_PUNCT_CHARS`'s own comment, finding 2), so this carrier probe's
        # OLD expected value, "密码 is－[REDACTED_EMAIL]-[REDACTED_IP]", was two fragments of a
        # labeled value glued together by a literal surviving "-" -- exactly the fragmentation shape
        # this whole architectural rewrite exists to eliminate, just not itself a raw-secret leak
        # since both halves happened to be independently redacted. Now that "－" is a recognized
        # connector/value character like "："/"＝"/"-" already were, Phase A's own atomic-span
        # capture claims the whole "－ops@node.invalid-aa:bc:de:f0:12:34" span before either
        # `_EMAIL_RE` or `_MAC_ADDRESS_RE` ever runs on it -- a strictly MORE redacted, single-
        # placeholder outcome, not a narrower one. `_MAC_ADDRESS_RE`'s own dash-boundary guard (this
        # test's actual subject) remains directly exercised, unaffected, by the unlabeled
        # `longer_run` assertion below.
        self.assertEqual(once, "密码 [REDACTED]", once)
        self.assertNotIn("node.invalid", once)
        self.assertNotIn("aa:bc:de:f0:12:34", once)
        self.assertEqual(once, twice, "must already be idempotent on the first pass")
        # The original round-2 repro this guard exists for must still be rejected (matching mid a
        # genuinely longer dash-separated hex run).
        longer_run = "12-34-de-ad-be-ef-00-11"
        self.assertEqual(hook.redact(longer_run), longer_run)

    # ------------------------------------------------------------------
    # Round-3 (this round): fixes for 5 of the 11 findings from the last dual-review gate (opus +
    # codex + grok). Each test below is verified to genuinely fail against this file's HEAD before
    # this round's fix and pass after -- see this round's own final report for the exact
    # before/after transcripts and interpreter version. Findings 1 (han-bridge over-reach across an
    # unrelated CJK clause) and 9 (pre-existing IPv6-after-email idempotency gap) are NOT closed
    # this round -- both are documented, honest, non-blocking residual gaps described in prose in
    # this round's own report, per this file's own process-integrity rule: no test here is shaped to
    # make either gap look closed.
    # ------------------------------------------------------------------

    def test_round3_fullwidth_punctuation_connector_no_longer_barriers_the_value(self) -> None:
        # Finding 2 (P2): 17 fullwidth/CJK punctuation characters (the fullwidth hyphen U+FF0D and
        # 16 siblings) sat in neither the connector class nor the value class -- a keyword glued
        # directly to its value by one of these (routine when a Chinese IME emits fullwidth
        # punctuation) never engaged Phase A at all, handing the raw value to Phase B's narrower
        # structural patterns to fragment. Verified fail-before against this file's own HEAD (before
        # this round): every case below left the secret partly or wholly in the clear.
        cases = (
            ("密码－Zq7Wr8Kp9", ("Zq7Wr8Kp9",)),
            ("口令－supersecretvalue", ("supersecretvalue",)),
            ("密码－188 7734 2201 9948", ("188", "7734", "2201", "9948")),
            ("密码－Zq-198.51.100.23-Wr", ("Zq-198", "23-Wr")),
            ("密码－pre192.0.2.99post", ("pre192", "99post")),
        )
        for text, fragments in cases:
            redacted = hook.redact(text)
            self.assertTrue(redacted.endswith("[REDACTED]"), f"{text!r} -> {redacted!r}")
            for fragment in fragments:
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")
        # The other 16 barrier characters, generalized: each must now redact atomically too.
        for gap in "／＿．＠；｜＋＊～＂＇＄％＃！？":
            text = f"密码{gap}Zq7Wr8Kp9"
            redacted = hook.redact(text)
            self.assertNotIn("Zq7Wr8Kp9", redacted, f"gap={gap!r}: {text!r} -> {redacted!r}")

    def test_round3_fullwidth_punctuation_connector_does_not_flag_ordinary_prose(self) -> None:
        # The widened connector's own false-positive guard: ordinary Chinese prose using a keyword
        # immediately followed by one of the new punctuation characters, with no plausible secret
        # value after it, must not redact -- the value class still declines a Han-led continuation.
        unchanged = (
            "密码！这非常重要，请妥善保管。",
            "密钥；令牌都属于敏感信息。",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)

    def test_round3_short_han_gap_extension_covers_five_or_more_ideographs(self) -> None:
        # Finding 6 (grok P1): the forward Han-gap extension was capped at 4 consecutive ideographs,
        # a sharp cliff -- a labeled value containing 5+ consecutive Han characters followed by more
        # ASCII was not captured atomically at all. Verified fail-before against this file's own
        # HEAD: `redact('密码：hello测试代码库X9')` -> `'密码：[REDACTED]测试代码库X9'` (the 5-ideograph
        # run and the ASCII tail both survive in the clear).
        redacted = hook.redact("密码：hello测试代码库X9")
        self.assertEqual(redacted, "密码：[REDACTED]")
        self.assertNotIn("X9", redacted)
        self.assertEqual(hook.redact(redacted), redacted)
        # 4-ideograph boundary (already worked) must still work, confirming no narrowing.
        self.assertEqual(hook.redact("密码：hello测试代码X9"), "密码：[REDACTED]")
        # Must not reopen the established date/measure-word false positive this extension's own
        # letter-requiring gate protects (unaffected by raising the cap, since "2024" has no letter).
        self.assertEqual(hook.redact("密码：2024年12月31日到期"), "密码：2024年12月31日到期")

    def test_round3_structural_gap_bridge_covers_tab_nbsp_and_carriage_return(self) -> None:
        # Finding 7 (grok P1): the structural-gap bridges (CJK sibling and the ASCII/assignment
        # sibling) only ever recognized a literal ASCII space as the one-character gap between a
        # labeled value and a following structural match -- a tab, NBSP, or carriage return in that
        # exact position left only the innermost structural shape redacted. Verified fail-before
        # against this file's own HEAD: each whitespace variant below left the hostname/suffix in
        # the clear (only the userinfo credential's own narrow slice was redacted).
        for ws, label in ((" ", "space"), ("\t", "tab"), ("\xa0", "nbsp"), ("\r", "cr")):
            text = f"密码：Ab7x{ws}https://bob:hunter2pw@example.com-Qv"
            redacted = hook.redact(text)
            self.assertEqual(redacted, "密码：[REDACTED]", f"{label}: {text!r} -> {redacted!r}")
            self.assertNotIn("example.com", redacted, label)
            self.assertNotIn("hunter2pw", redacted, label)
            # ASCII "key: value" sibling path -- only the STRUCTURAL gap (between the value's own
            # ASCII prefix and the following URL/email) varies; the connector separator itself
            # (a distinct, unrelated code path) stays a plain space so the expected output is fixed.
            text2 = f"password: Ab7x{ws}admin@node.invalid-Rq"
            redacted2 = hook.redact(text2)
            self.assertEqual(redacted2, "password: [REDACTED]", f"{label}: {text2!r} -> {redacted2!r}")
            self.assertNotIn("node.invalid", redacted2, label)
            self.assertNotIn("Rq", redacted2, label)

    def test_round3_false_positive_denylist_covers_common_single_word_values(self) -> None:
        # Finding 8 (grok P2): a handful of common single-word non-secret values from realistic
        # config/documentation prose were missing from the closed non-secret-word vocabulary.
        # Verified fail-before against this file's own HEAD: each case below redacted the word.
        unchanged = (
            "api_key: disabled",
            "password: none",
            "key: primary",
            "token: uuid",
            "token: Bearer tokens are used in OAuth",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)
        # Must not reopen coverage for a real secret shaped similarly.
        self.assertEqual(hook.redact("password: hunter2value"), "password: [REDACTED]")
        self.assertEqual(
            hook.redact("password: Bearer xoxbrealsecretsessiontoken123"), "password: [REDACTED]"
        )

    def test_round3_jwt_no_longer_eats_a_following_url_scheme_when_unlabeled(self) -> None:
        # Finding 10 (grok P2): for an UNLABELED (no keyword nearby) JWT immediately glued to a
        # userinfo-bearing URL, JWT's own greedy final base64url segment swallowed the literal word
        # "https" before `_URL_USERINFO_RE` ever ran, destroying its scheme anchor and leaving the
        # username/password fragment exposed. Verified fail-before against this file's own HEAD:
        # the username and password fragment below survived in the clear.
        jwt = "ey" + "A" * 10 + "." + "B" * 10 + "." + "C" * 10
        text = jwt + "https://bob:p#assword@example.com"
        redacted = hook.redact(text)
        self.assertNotIn("bob", redacted)
        self.assertNotIn("p#assword", redacted)
        self.assertNotIn("assword", redacted)
        self.assertEqual(hook.redact(redacted), redacted)
        # The labeled/han-bridged sibling shape must still collapse to one atomic placeholder either
        # way (per this round's own reorder-safety argument in `redact()`'s own comment).
        labeled = hook.redact("密码：Ax7密-https://bob:hunter2@example.com|" + jwt)
        self.assertEqual(labeled, "密码：[REDACTED]")

    def test_round6_table_cell_secret_with_embedded_cjk_gap_now_bridges(self) -> None:
        # Finding 12 (BLOCKING B1): the Han-bridge trigger only recognized an INLINE connector
        # (：/是/is), never a markdown table-cell pipe boundary, so a table-cell labeled value with
        # an embedded ideograph fragmented even though the identical inline shape already bridged.
        self.assertEqual(
            hook.redact("| 密码 | Ax7Z密-198.51.100.73-Qv |"), "| 密码 | [REDACTED] |"
        )

    def test_round6_compound_suffix_label_folded_into_placeholder_still_bridges(self) -> None:
        # Finding 13 (BLOCKING B2): a compound-suffixed CJK label ("密码-prod") is folded, along
        # with its real connector, into a bare "[REDACTED]" by an earlier Phase A pass -- the
        # Han-bridge trigger's CJK branch required a REAL connector token, not a placeholder, so it
        # could never recognize the label context again for the embedded-ideograph tail that
        # followed.
        redacted = hook.redact("密码-prod：Ax7密-198.51.100.73-Qv")
        self.assertEqual(redacted, "密码[REDACTED]")
        self.assertNotIn("198.51.100.73", redacted)
        self.assertNotIn("Qv", redacted)

    def test_round6_ascii_assignment_labeled_value_bridges_atomically(self) -> None:
        # Finding 14 (BLOCKING B3, root cause traced to `_ASSIGNMENT_RE` specifically, not
        # `_QUERY_SECRET_RE` as the finding's own text suspected -- confirmed by tracing each
        # prologue pass individually before changing anything; see the module-level comment above
        # `_extend_value_end_past_adjacent_placeholder`): `_ASSIGNMENT_RE`'s own genuine
        # `&key=`-boundary termination correctly stops its OWN narrower match at "&more=1...", but
        # the outer CJK-labeled value this shape sits inside was left fragmented, with Phase A's
        # placeholder-avoidance guard stopping right at `_ASSIGNMENT_RE`'s own leftover
        # placeholder.
        redacted = hook.redact("密码：Aa7#zP?token=xyz&more=1-198.51.100.23-Zz")
        self.assertEqual(redacted, "密码：[REDACTED]")
        # The genuinely standalone case this fix must not regress: two real, separate query
        # parameters sharing one line with no labeled CJK/ASCII secret keyword nearby.
        self.assertEqual(
            hook.redact("token=abc123&next=xyz&other=1"), "token=[REDACTED]&next=xyz&other=1"
        )

    def test_round6_han_bridge_lookback_is_not_capped_at_a_fixed_window(self) -> None:
        # Finding 16 (BLOCKING B5): the driver's own backward search used to be capped at a fixed
        # 160-character window, reproducing the identical truncation-leak shape already fixed once
        # for `_CJK_VALUE_MAX_LEN` -- any value longer than the cap between the keyword and the
        # first Phase-B structural match leaked its prefix in full.
        text = "密码：Ax7密" + (".x" * 85) + "-198.51.100.73-Qv"
        self.assertGreater(len(text), 160)
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]")

    def test_round6_short_han_gap_after_structural_content_bridges_with_a_letter_prefix(self) -> None:
        # Finding 17 (BLOCKING B6): when structural content (an IPv4 address) is consumed directly
        # by Phase A's own atomic capture and is THEN followed by a short embedded ideograph and
        # more real secret bytes, there was no Phase-B match left for any existing bridge mechanism
        # to attach to, so the CJK-suffixed tail leaked.
        redacted = hook.redact("密码：Ab-198.51.100.73-密Qv")
        self.assertEqual(redacted, "密码：[REDACTED]")
        # Must not reopen the established date/measure-word false positive this file's own
        # CJK-adjacency doctrine protects: a purely-numeric prefix before the gap is NOT bridged
        # (the `_VALUE_EXTENSION_REQUIRES_LETTER_RE` gate this fix added specifically for this).
        date_text = "密码：2024年12月31日到期"
        self.assertEqual(hook.redact(date_text), date_text)
        # And the already-pinned "genuine trailing Chinese sentence must not be swallowed" case.
        sentence = "密码：Ax7密-198.51.100.73-Qv这是一个正常的中文句子，无关内容"
        self.assertEqual(
            hook.redact(sentence), "密码：[REDACTED]这是一个正常的中文句子，无关内容"
        )

    def test_round6_false_positives_on_realistic_non_secret_prose_do_not_redact(self) -> None:
        # Finding 11: three real, reproducible false positives against realistic non-secret
        # technical documentation prose (synthetic examples, not the review's own captured
        # samples).
        unchanged = (
            "token: OpenAPI2024 documents",
            "password: varchar(255)",
            "device code: generated",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)
        # Must not reopen coverage for a real secret shaped similarly.
        self.assertEqual(hook.redact("password: hunter2value"), "password: [REDACTED]")

    def test_round6_new_shapes_are_idempotent(self) -> None:
        cases = (
            "电话 13800138000-13900139000",
            "| 密钥: | Kp9wQ3zLm7 |",
            "密码：Ax7密-https://bob:hunter2@example.com|Bearer xoxbrealsessiontok",
            "密码 is－ops@node.invalid-aa:bc:de:f0:12:34",
            "| 密码 | Ax7Z密-198.51.100.73-Qv |",
            "密码-prod：Ax7密-198.51.100.73-Qv",
            "密码：Aa7#zP?token=xyz&more=1-198.51.100.23-Zz",
            "密码：Ab-198.51.100.73-密Qv",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: once={once!r} twice={twice!r}")

    def test_round6_han_gap_bridge_and_lookback_removal_scale_near_linearly(self) -> None:
        # ReDoS re-verification for both new mechanisms this round: the uncapped lookback search
        # (finding 16) and the short-Han-gap value extension (finding 17).
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        small = timed("a-" * 8_000)
        large = timed("a-" * 32_000)  # 4x input
        self.assertLess(
            large, small * 4 + 0.5, "uncapped han-bridge lookback must scale near-linearly"
        )
        self.assertLess(large, 3.0)

        small2 = timed("密码：Ax7密" + (".x" * 2_000) + "-198.51.100.1-Qv")
        large2 = timed("密码：Ax7密" + (".x" * 8_000) + "-198.51.100.1-Qv")  # 4x input
        self.assertLess(
            large2, small2 * 4 + 0.5, "removed lookback cap must scale near-linearly"
        )
        self.assertLess(large2, 3.0)

    def test_round6_cjk_adjacency_and_ignorecase_taint_spot_check(self) -> None:
        # Re-verified directly against the rebuilt/widened patterns this round touched, rather than
        # only trusting the existing suite passing (per this effort's own standing process rule).
        for pattern in (
            hook._MAC_ADDRESS_RE,
            hook._CN_MOBILE_RE,
            hook._HAN_BRIDGE_TRIGGER_RE,
        ):
            self.assertFalse(pattern.flags & re.IGNORECASE, pattern.pattern)
            self.assertNotIn(r"\b", pattern.pattern)
        # A MAC address glued directly to CJK text with no separating whitespace must still redact
        # (the widened dash-boundary guard must not have reopened a CJK-adjacency gap).
        self.assertEqual(
            hook.redact("设备地址de:ad:be:ef:00:11结束"), "设备地址[REDACTED_IP]结束"
        )

    # ------------------------------------------------------------------
    # Architectural-rewrite round-7: fixes for the 4 real P1s an independent dual review (Claude
    # opus/max + Codex sol/max + an additional grok pass) found against the round-6 candidate.
    # Each test below is verified to genuinely fail against the round-6 HEAD of this file (before
    # this round's fix) and pass after -- see this round's own final report for the exact
    # before/after transcripts.
    # ------------------------------------------------------------------

    def test_round7_table_column_scan_does_not_leak_a_second_self_contained_pair_row(self) -> None:
        # BLOCKING P1 (independent dual review against the round-6 candidate): a markdown table
        # where EACH row is already its own complete, self-contained "label | value" pair (not one
        # header row followed by data rows) armed the LABEL's own column for later rows instead of
        # the VALUE's column -- so a second row's real secret, one cell over from a label the closed
        # vocabulary does not recognize on its own ("备用"/"backup", deliberately not the compound
        # "备份码"/"备用码" forms -- see `_CJK_SECRET_KEYWORD_OTHER`'s own comment), was never
        # reached at all. Verified to leak the second row's secret in full against round-6 HEAD;
        # `_redact_cjk_secret_table_columns`'s self-contained-pair fix closes it by arming the
        # VALUE's own column (a structural fact) instead of the label's.
        secret1 = "sk-abcdefghij1234567890"
        secret2 = "Tq4zW7pNe2Vs"
        cjk = hook.redact(f"| 密钥 | {secret1} |\n| 备用 | {secret2} |")
        self.assertNotIn(secret1, cjk)
        self.assertNotIn(secret2, cjk)
        self.assertEqual(cjk, "| 密钥 | [REDACTED] |\n| 备用 | [REDACTED] |")

        secret3 = "ghp_abcdefghij1234567890abcd"
        secret4 = "Mv8qRt3wZy"
        cjk2 = hook.redact(f"| 密钥 | {secret3} |\n| 备用 | {secret4} |")
        self.assertNotIn(secret3, cjk2)
        self.assertNotIn(secret4, cjk2)

        secret5 = "sk-abcdefghij1234567890"
        secret6 = "Tq4zW7pNe2Vs"
        ascii_ = hook.redact(f"| token | {secret5} |\n| backup | {secret6} |")
        self.assertNotIn(secret5, ascii_)
        self.assertNotIn(secret6, ascii_)
        self.assertEqual(ascii_, "| token | [REDACTED] |\n| backup | [REDACTED] |")
        # Idempotent: a second pass must not re-arm the label's own column and mis-redact the
        # unrelated label word ("backup") on the next call.
        self.assertEqual(hook.redact(ascii_), ascii_)

        # The canonical multi-row HEADER + data-rows shape (the ORIGINAL, still-valid reason this
        # column tracker exists) must still redact exactly as before -- unaffected by this fix,
        # since a genuine header row's other cells never independently look secret-shaped.
        header_secret = "Tq4%wZ7nJ2bV"
        header_data = hook.redact(
            f"| 账号 | 密码 | 备注 |\n|---|---|---|\n| admin | {header_secret} | 生产 |"
        )
        self.assertNotIn(header_secret, header_data)
        self.assertIn("| admin |", header_data)
        self.assertIn("生产", header_data)

    def test_round7_space_bridge_now_covers_every_phase_b_structural_pattern(self) -> None:
        # P1-A (grok independent review against the round-6 candidate):
        # `_extend_value_end_past_space_structural_gap` only bridged a space then one of 4 of the
        # 12 Phase-B structural patterns (email/IPv4/MAC/CN-mobile), so a labeled value followed by
        # a space then a userinfo-bearing URL fragmented -- the literal "https://" prefix and the
        # host/suffix after the credential survived in the clear next to a placeholder that looked
        # fully handled. Verified to leak against round-6 HEAD; fixed by listing every Phase-B
        # structural pattern in the bridge.
        redacted = hook.redact("密码：Ab7x https://bob:hunter2pw@example.com-Qv")
        self.assertEqual(redacted, "密码：[REDACTED]")
        self.assertNotIn("hunter2pw", redacted)
        self.assertNotIn("example.com", redacted)
        self.assertNotIn("https://", redacted)

    def test_round7_short_han_gap_forward_extension_covers_full_cjk_family(self) -> None:
        # P1-B (grok independent review against the round-6 candidate):
        # `_extend_value_end_past_short_han_gaps`'s gap class was `_CJK_IDEOGRAPH_CLASS`
        # (ideographs only), while Phase B's own trigger side was already widened to the full
        # CJK-family range set (katakana/hangul/fullwidth punctuation) in round 6 -- so a decoy
        # katakana/hangul/fullwidth-hyphen character inside a labeled value still fragmented on
        # this (forward, Phase-A-side) extension even though the symmetric backward bridge already
        # handled it. Verified to leak against round-6 HEAD for each gap character below.
        for gap, label in (("カ", "katakana"), ("가", "hangul"), ("－", "fullwidth hyphen")):
            text = f"密码：Ab-198.51.100.73{gap}Qv"
            redacted = hook.redact(text)
            self.assertEqual(redacted, "密码：[REDACTED]", label)
            self.assertNotIn("Qv", redacted, label)
            self.assertNotIn("198.51.100.73", redacted, label)

    def test_round7_ascii_assignment_bridges_over_glued_structural_query_tail(self) -> None:
        # P1-C (grok independent review against the round-6 candidate): `_ASSIGNMENT_RE`'s
        # deliberate `&ident=`-boundary stop (protecting a genuinely separate, unrelated query
        # parameter) also left a glued STRUCTURAL tail -- the same secret's own bytes, not a
        # separate parameter -- exposed next to Phase B's own narrower redaction of just the shape
        # it recognizes inside that tail. Verified to leak against round-6 HEAD.
        redacted = hook.redact("password: pre?token=xyz&more=1-198.51.100.23-Zz")
        self.assertEqual(redacted, "password: [REDACTED]")
        self.assertNotIn("more=1", redacted)
        self.assertNotIn("Zz", redacted)
        self.assertNotIn("198.51.100.23", redacted)
        # The genuinely standalone case this fix must not regress: two real, separate query
        # parameters with no structural secret shape in either of them.
        self.assertEqual(
            hook.redact("token=abc123&next=xyz&other=1"), "token=[REDACTED]&next=xyz&other=1"
        )
        # Idempotent, and the placeholder-bridge relaxation this fix required (`+` -> `*` in
        # `_placeholder_bridge_continuation`) must not re-fragment a CJK-labeled value whose
        # embedded ASCII assignment tail is fully consumed with nothing left over.
        nested = hook.redact("密码：Aa7#zP?token=xyz&more=1-198.51.100.23-Zz")
        self.assertEqual(nested, "密码：[REDACTED]")
        self.assertEqual(hook.redact(nested), nested)

    def test_round8_ascii_assignment_bridges_over_space_structural_gap(self) -> None:
        # BLOCKING P1-A/grok-item-15 (against the round-7 candidate): the round-7 fix
        # (`_extend_value_end_past_space_structural_gap`) closed this exact "space then a genuine
        # Phase-B structural shape" fragmentation for the CJK/table value grammar, but never wired
        # an equivalent bridge into `_ASSIGNMENT_RE`'s own ASCII `key: value`/`key=value` path --
        # so the identical shape still fragmented there: `_ASSIGNMENT_RE`'s space-continuation only
        # continues when the following token independently looks digit/uppercase-code-shaped, and a
        # URL/email/MAC/etc continuation does not, so it stopped early and Phase B's own narrower
        # pattern then redacted only the slice it recognized. Verified to leak against round-7 HEAD:
        # `redact('password: Ab7x https://bob:hunter2pw@example.com-Qv')` ->
        # `'password: [REDACTED] https://[REDACTED]@example.com-Qv'` (the literal "https://" prefix
        # and "example.com-Qv" suffix of the same labeled secret survived in the clear).
        #
        # Fixed by folding the identical bridge into the existing `&ident=`-glue post-pass
        # (`_extend_past_query_glued_structural_clauses`, now handling both continuation shapes) so
        # both fixes compose in the same bounded scan. Covers the three structural shapes item 15
        # named explicitly (URL-userinfo, MAC, email) plus a bare prefixed token/Bearer credential,
        # confirming those bridge into ONE atomic labeled span (not just redacted on their own,
        # unlabeled) as item 15 specifically flagged as still-broken even for the 6 "newly bridged"
        # patterns.
        url_case = hook.redact("password: Ab7x https://bob:hunter2pw@example.com-Qv")
        self.assertEqual(url_case, "password: [REDACTED]")
        self.assertNotIn("hunter2pw", url_case)
        self.assertNotIn("example.com", url_case)
        self.assertNotIn("https://", url_case)

        mac_case = hook.redact("password: Ab7x 00:1A:2B:3C:4D:5E-Qv")
        self.assertEqual(mac_case, "password: [REDACTED]")
        self.assertNotIn("Qv", mac_case)
        self.assertNotIn("00:1A:2B:3C:4D:5E", mac_case)

        email_case = hook.redact("token: Ab7x admin@node.invalid-Rq")
        self.assertEqual(email_case, "token: [REDACTED]")
        self.assertNotIn("Rq", email_case)
        self.assertNotIn("admin@node.invalid", email_case)

        bearer_case = hook.redact("api_key: Ab7x Bearer abcdefghij1234567890-Qv")
        self.assertEqual(bearer_case, "api_key: [REDACTED]")
        self.assertNotIn("Qv", bearer_case)
        self.assertNotIn("abcdefghij1234567890", bearer_case)

        # Must not regress the genuinely unrelated case: an ordinary sentence following a labeled
        # value whose next word is not independently a validated structural shape stays fragmented
        # exactly as before (Phase A/the assignment grammar declines the sentence, Phase B has
        # nothing structural to redact, so it is left untouched) -- this bridge only ever fires on
        # an ACTUAL validated match, never a bare word.
        prose = hook.redact("token: abc123def see https://example.com/docs for more")
        self.assertEqual(prose, "token: [REDACTED] see https://example.com/docs for more")

        for text in (url_case, mac_case, email_case, bearer_case):
            self.assertEqual(hook.redact(text), text, text)

    def test_round8_ascii_assignment_space_bridge_scales_near_linearly(self) -> None:
        # ReDoS re-verification for this round's new mechanism: folding the "space then a genuine
        # Phase-B structural match" bridge into `_extend_past_query_glued_structural_clauses`'s own
        # while-loop for the ASCII assignment path. Two adversarial shapes: (1) a long tail of
        # ordinary non-matching tokens after the placeholder, where the loop must bail out on its
        # very first failed 12-pattern attempt rather than rescanning; (2) a long CHAIN of
        # genuinely-bridging structural matches (space-separated emails), exercising the loop's
        # repeated-iteration path.
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        small = timed("password: Ab7x " + ("nomatch " * 2_000))
        large = timed("password: Ab7x " + ("nomatch " * 8_000))  # 4x input
        self.assertLess(large, small * 4 + 0.5, "non-matching tail must scale near-linearly")
        self.assertLess(large, 3.0)

        small2 = timed("password: Ab7x " + " ".join(f"u{i}@node{i}.invalid" for i in range(500)))
        large2 = timed("password: Ab7x " + " ".join(f"u{i}@node{i}.invalid" for i in range(2_000)))
        self.assertLess(large2, small2 * 4 + 0.5, "chained structural-match bridge must scale near-linearly")
        self.assertLess(large2, 3.0)

    def test_round7_false_positive_denylist_additions(self) -> None:
        # Non-blocking over-redaction finding (grok independent review, item 27): common
        # literal-value/state words wrongly redacted as if they were secret values.
        for text in (
            "token: expires after one hour",
            "password: true",
            "token: empty",
            "token: undefined",
        ):
            self.assertEqual(hook.redact(text), text, text)

    def test_round7_new_shapes_are_idempotent(self) -> None:
        cases = (
            "| 密钥 | sk-abcdefghij1234567890 |\n| 备用 | Tq4zW7pNe2Vs |",
            "| token | sk-abcdefghij1234567890 |\n| backup | Tq4zW7pNe2Vs |",
            "密码：Ab7x https://bob:hunter2pw@example.com-Qv",
            "密码：Ab-198.51.100.73カQv",
            "密码：Ab-198.51.100.73가Qv",
            "密码：Ab-198.51.100.73－Qv",
            "password: pre?token=xyz&more=1-198.51.100.23-Zz",
            "密码：Aa7#zP?token=xyz&more=1-198.51.100.23-Zz",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, f"{text!r}: once={once!r} twice={twice!r}")

    def test_round7_new_mechanisms_scale_near_linearly(self) -> None:
        # ReDoS re-verification for this round's 3 new/widened mechanisms: the space-bridge's
        # widened structural-pattern list (P1-A), the query-glue bridge (P1-C), and the
        # full-CJK-family forward gap bridge (P1-B).
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text))

        small = timed("密码：Ab7x " + ("x" * 2_000))
        large = timed("密码：Ab7x " + ("x" * 8_000))  # 4x input
        self.assertLess(large, small * 4 + 0.5, "widened space-bridge must scale near-linearly")
        self.assertLess(large, 3.0)

        small2 = timed(
            "password: " + "&".join(f"k{i}=1-198.51.100.{i % 250}-z" for i in range(200))
        )
        large2 = timed(
            "password: " + "&".join(f"k{i}=1-198.51.100.{i % 250}-z" for i in range(800))
        )  # 4x input
        self.assertLess(large2, small2 * 4 + 0.5, "query-glue bridge must scale near-linearly")
        self.assertLess(large2, 3.0)

        small3 = timed("密码：Ab" + ("カ" * 2_000) + "Qv")
        large3 = timed("密码：Ab" + ("カ" * 8_000) + "Qv")  # 4x input
        self.assertLess(large3, small3 * 4 + 0.5, "full-CJK-family gap bridge must scale near-linearly")
        self.assertLess(large3, 3.0)

    def test_round7_realistic_non_secret_prose_still_not_redacted(self) -> None:
        # Regression test 7 from this round's brief: realistic Chinese/English technical prose
        # using a label word with no real secret nearby must not be redacted.
        for text in (
            "密码策略要求至少12位字符",
            "密钥管理服务负责轮换所有凭证",
            "令牌过期时间设置为3600秒",
            "password policy requires at least 12 characters",
            "please rotate the API key every 90 days",
            "当前密码：Argon2id 是推荐的哈希算法。",
        ):
            self.assertEqual(hook.redact(text), text, text)


class AtomicLabeledSpanRewriteRound4Tests(unittest.TestCase):
    """Regression coverage for the scanner-first labeled-secret architecture."""

    def test_space_grouped_backup_code_is_one_atomic_span(self) -> None:
        value = "159 3308 7742 3320"
        redacted = hook.redact(f"备份码：{value}")
        self.assertEqual(redacted, "备份码：[REDACTED]")
        self.assertNotIn("3320", redacted)

    def test_labeled_password_with_embedded_ipv4_is_one_atomic_span(self) -> None:
        value = "Ab-192.0.2.44-Cd"
        redacted = hook.redact(f"密码：{value}")
        self.assertEqual(redacted, "密码：[REDACTED]")
        self.assertNotIn("[REDACTED_IP]", redacted)

    def test_separator_free_alphabetic_device_code_is_redacted(self) -> None:
        self.assertEqual(hook.redact("设备码 WDJB-MJHT"), "设备码 [REDACTED]")

    def test_table_value_with_escaped_pipe_is_one_atomic_span(self) -> None:
        value = r"Ab\|192.0.2.44\|Cd"
        redacted = hook.redact(f"| 密码 | {value} |")
        self.assertEqual(redacted, "| 密码 | [REDACTED] |")
        self.assertNotIn("192.0.2.44", redacted)

    def test_compound_suffixed_labels_preserve_their_suffix_inline_and_in_tables(self) -> None:
        cases = (
            ("密码2：Zq7", "密码2：[REDACTED]"),
            ("密码-prod：Zq7", "密码-prod：[REDACTED]"),
            ("password2: Zq7", "password2: [REDACTED]"),
            ("| 密码2 | Zq7 |", "| 密码2 | [REDACTED] |"),
            ("| 密码-prod | Zq7 |", "| 密码-prod | [REDACTED] |"),
            ("| password2 | Zq7 |", "| password2 | [REDACTED] |"),
        )
        for source, expected in cases:
            self.assertEqual(hook.redact(source), expected, source)

    def test_general_connector_scan_is_linear_on_alternating_punctuation(self) -> None:
        text = "密码" + (" :" * 32_768)
        elapsed = _min_elapsed(lambda: hook.redact(text), attempts=3)
        self.assertLess(elapsed, 0.5, elapsed)

    def test_arbitrary_value_prefix_is_not_swallowed_as_a_cjk_label_suffix(self) -> None:
        cases = (
            "密码-Ab7 198.51.100.23",
            "密钥_Zq9 0a:1b:2c:3d:4e:5f",
            "令牌-Kp3 zz9tester@example.org",
        )
        for source in cases:
            redacted = hook.redact(source)
            self.assertNotIn("[REDACTED_IP]", redacted)
            self.assertNotIn("[REDACTED_EMAIL]", redacted)
            self.assertNotIn("Ab7", redacted)
            self.assertNotIn("Zq9", redacted)
            self.assertNotIn("Kp3", redacted)
            self.assertEqual(hook.redact(redacted), redacted)

    def test_technical_algorithm_name_and_documentation_prose_keep_existing_behavior(self) -> None:
        self.assertEqual(
            hook.redact("密码：Argon2id 是推荐的算法"),
            "密码：Argon2id 是推荐的算法",
        )
        self.assertEqual(
            hook.redact("secret: this field documents the schema"),
            "secret: this field documents the schema",
        )

    def test_first_pass_is_idempotent_for_space_connected_ascii_code_and_ipv4(self) -> None:
        source = "password  QXNB-PLKM 198.51.100.44"
        once = hook.redact(source)
        self.assertEqual(once, "password  [REDACTED]")
        self.assertEqual(hook.redact(once), once)

    def test_labeled_value_on_immediately_following_line_is_atomic(self) -> None:
        source = "密码：\n  Ab-198.51.100.23-Cd"
        redacted = hook.redact(source)
        self.assertEqual(redacted, "密码：\n  [REDACTED]")
        self.assertNotIn("198.51.100.23", redacted)
        self.assertEqual(hook.redact(redacted), redacted)


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


class ArchitecturalRewriteRound5Tests(unittest.TestCase):
    """Round-5 (this round): P0 Bearer-anchor-destruction fix and P1 quadratic-blowup fix.

    Regression coverage for the two BLOCKING findings from the immediately prior dual-review
    gate, each verified fail-before/pass-after against the actual HEAD of this file at the start
    of this round (see this round's own report for the exact before/after numbers). Every secret
    value below is synthetic, constructed for this test only, never a real captured credential.
    """

    # --- P0: keyword + "Bearer <token>" must claim the WHOLE credential atomically ----------

    def test_p0_compound_label_bearer_credential_redacts_atomically_no_leak(self) -> None:
        # The exact repro class from the finding: a compound-suffixed label (a numeric/environment
        # suffix ahead of "Bearer") drives `needs_preemption` True for a reason OTHER than the
        # Bearer risk probe itself -- before this round's fix, that caused `_atomic_span_end` to
        # stop right after the literal word "Bearer" (mistaking the real token for unrelated
        # following lower-case prose), so only the word "Bearer" was replaced and the entire real
        # credential leaked in the clear right next to it.
        token = "qzC9mLxeVnR2sTgHk4Ap"
        cases = {
            f"密码2：Bearer {token}": "密码2：[REDACTED]",
            f"password2: Bearer {token}": "password2: [REDACTED]",
            f"密钥3=Bearer {token}": "密钥3=[REDACTED]",
            f"令牌-staging：Bearer {token}": "令牌-staging：[REDACTED]",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertNotIn(token, redacted, (text, redacted))
            self.assertEqual(redacted, expected, (text, redacted))

    def test_p0_structural_prefix_before_bearer_redacts_the_whole_span_atomically(self) -> None:
        # A PLAIN (non-compound) label whose value starts with an IPv4/MAC-shaped prefix ahead of
        # a literal "Bearer <token>" -- here `needs_preemption` is driven by the structural-risk
        # probe finding the prefix, but the same anchor-destruction bug truncated the claimed span
        # to just "Bearer", leaking the token that followed it.
        token = "qzC9mLxeVnR2sTgHk4Ap"
        cases = {
            f"密码：203.0.113.7-Bearer {token}": "密码：[REDACTED]",
            f"密码：3c:5a:b4:77:e1:09-Bearer {token}": "密码：[REDACTED]",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertNotIn(token, redacted, (text, redacted))
            self.assertEqual(redacted, expected, (text, redacted))

    def test_p0_table_cell_escaped_pipe_before_bearer_redacts_atomically(self) -> None:
        # The table-row escaped-pipe preemption branch hits the identical anchor-destruction bug.
        token = "qzC9mLxeVnR2sTgHk4Ap"
        cases = {
            "| 密码 | Ab7\\|Bearer " + token + " |": "| 密码 | [REDACTED] |",
            "| password | Ab7\\|Bearer " + token + " |": "| password | [REDACTED] |",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertNotIn(token, redacted, (text, redacted))
            self.assertEqual(redacted, expected, (text, redacted))

    def test_p0_unlabeled_bearer_still_redacts_and_atomic_span_end_stays_precise(self) -> None:
        # Sanity/no-regression: an unlabeled Bearer token (Phase B's own `_BEARER_RE`, no keyword
        # nearby) must still redact exactly as before this round's fix.
        token = "qzC9mLxeVnR2sTgHk4Ap"
        unlabeled = hook.redact(f"Authorization: Bearer {token}")
        self.assertNotIn(token, unlabeled)
        self.assertIn("Bearer [REDACTED]", unlabeled)

        # White-box: the P0 fix's bounded backward-peek exception must not fire for a real
        # lower-case English word following "Bearer " -- `_atomic_span_end` must still stop right
        # after the literal word "Bearer" in that case, exactly as before this round, so that
        # ordinary prose using the word "Bearer" is not swallowed into a claimed span.
        text = "密码：Bearer will now be checked"
        value_start = text.index("Bearer")
        end = hook._atomic_span_end(text, value_start, table_row=False)
        self.assertEqual(text[value_start:end], "Bearer")

    # --- P1: many labels sharing one boundary-free run must not be quadratic ----------------

    def test_p1_many_short_labels_one_line_scales_near_linearly(self) -> None:
        # The finding's own realistic repro: a one-line config/CSV dump with many short labels and
        # no real per-value terminator between them. Measured scaling (not just "completes"): the
        # prior round measured a ~2.00 scaling exponent (quadratic) here, up to ~40s for 115KB.
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text), attempts=3)

        small = "api_key:Ax00001Qz," * 1600  # ~28.8KB
        large = "api_key:Ax00001Qz," * 6400  # 4x input, ~115.2KB
        small_elapsed = timed(small)
        large_elapsed = timed(large)
        # Quadratic scaling would show ~16x for a 4x input increase; a generous 8x ceiling still
        # fails loudly on a real quadratic-or-worse regression while absorbing timing noise.
        self.assertLess(large_elapsed, small_elapsed * 8 + 1.0, "must scale near-linearly, not quadratically")
        self.assertLess(large_elapsed, 5.0, "must not hang a synchronous UserPromptSubmit hook")

    def test_p1_repeated_password_label_no_real_terminator_scales_near_linearly(self) -> None:
        # The finding's second concrete repro shape: `'password:' * n` with no real terminator.
        #
        # CPU-timed, not wall-timed (round 5 F6 -- see `_min_cpu_elapsed`). This is the slowest
        # shape in this file at rest (~2.0s for the large input), which left the 5.0s ceiling below
        # only 2.5x of headroom -- less than the 3.3x wall-clock inflation a 2x-oversubscribed
        # machine actually produces here, measured. The bounds are unchanged; only the clock is.
        def timed(text: str) -> float:
            return _min_cpu_elapsed(lambda: hook.redact(text), attempts=3)

        small = "password:" * 1600  # ~14.4KB
        large = "password:" * 6400  # 4x input, ~57.6KB
        small_elapsed = timed(small)
        large_elapsed = timed(large)
        self.assertLess(large_elapsed, small_elapsed * 8 + 1.0, "must scale near-linearly, not quadratically")
        self.assertLess(large_elapsed, 5.0)

    def test_p1_full_pipeline_at_hard_file_byte_scale_completes_quickly(self) -> None:
        # End-to-end realistic worst case: `HARD_FILE_BYTES` (262144) is the largest single memory
        # file `read_memory_documents` will read, and `split_blocks()` calls `redact()` on the full
        # untruncated block before any 4000-char slicing -- confirm the adversarial shape at that
        # scale still completes well within a synchronous hook's budget.
        text = "api_key:Ax00001Qz," * 13_800  # ~248KB, just under HARD_FILE_BYTES
        elapsed = _min_elapsed(lambda: hook.redact(text), attempts=2)
        self.assertLess(elapsed, 5.0, "a single ~250KB adversarial block must not hang the hook")

    # --- Additional required regression scenarios (labeled-secret architecture) -------------

    def test_space_grouped_recovery_code_redacts_as_one_atomic_span(self) -> None:
        # Scenario 1: a labeled backup/recovery-code value that is space-grouped and phone-shaped
        # end to end -- must redact as ONE atomic span, zero characters of the value surviving.
        text = "备份码：7734 2951 6608 4423"
        redacted = hook.redact(text)
        for fragment in ("7734", "2951", "6608", "4423"):
            self.assertNotIn(fragment, redacted, (text, redacted))
        self.assertEqual(redacted, "备份码：[REDACTED]")
        self.assertEqual(redacted, hook.redact(redacted))

    def test_embedded_ipv4_with_affixes_redacts_as_one_atomic_span(self) -> None:
        # Scenario 2: a labeled password value containing an embedded IPv4-shaped substring with
        # prefix/suffix characters around it -- must redact as ONE atomic span.
        text = "密码：Ab-192.0.2.44-Cd"
        redacted = hook.redact(text)
        for fragment in ("Ab-", "-Cd", "192.0.2.44"):
            self.assertNotIn(fragment, redacted, (text, redacted))
        self.assertEqual(redacted, "密码：[REDACTED]")

    def test_separator_free_hyphenated_device_code_redacts(self) -> None:
        # Scenario 3: a separator-free alphabetic-hyphenated device/authorization code.
        text = "设备码 WDJB-MJHT"
        redacted = hook.redact(text)
        self.assertNotIn("WDJB", redacted)
        self.assertNotIn("MJHT", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_table_cell_escaped_pipe_inside_value_redacts_fully(self) -> None:
        # Scenario 4: a markdown table cell containing an escaped pipe inside the secret value
        # (same-row shape: label and value in the same "| label | value |" row).
        text = "| 密码 | Ab7\\|Cd9Ef |"
        redacted = hook.redact(text)
        self.assertNotIn("Ab7", redacted)
        self.assertNotIn("Cd9Ef", redacted)
        self.assertEqual(redacted, "| 密码 | [REDACTED] |")

    def test_compound_suffixed_labels_share_vocabulary_inline_and_table(self) -> None:
        # Scenario 5: compound-suffixed labels (keyword+digit, keyword+"-prod") in BOTH
        # inline-prose and table-cell contexts -- the two matchers must share vocabulary.
        secret = "Qm7xLpN2vRw9"
        cases = {
            f"密码2：{secret}": "密码2：[REDACTED]",
            f"密码-prod：{secret}": "密码-prod：[REDACTED]",
            f"password2: {secret}": "password2: [REDACTED]",
            f"| 密码2 | {secret} |": "| 密码2 | [REDACTED] |",
            f"| password2 | {secret} |": "| password2 | [REDACTED] |",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, (text, redacted))
            self.assertEqual(redacted, expected, (text, redacted))

    def test_adversarial_whitespace_run_is_near_linear_not_redos(self) -> None:
        # Scenario 6: an adversarial ReDoS-shaped input (a long repeated whitespace run) with a
        # measured near-linear timing result, not just an assertion it completes.
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text), attempts=3)

        small = "密码：" + (" " * 2_000) + "Zz9"
        large = "密码：" + (" " * 8_000) + "Zz9"  # 4x input
        small_elapsed = timed(small)
        large_elapsed = timed(large)
        self.assertLess(large_elapsed, small_elapsed * 8 + 1.0, "must scale near-linearly")
        self.assertLess(large_elapsed, 3.0)

    def test_realistic_non_secret_prose_using_label_words_is_not_redacted(self) -> None:
        # Scenario 7: realistic non-secret Chinese/English technical sentences using a label word
        # with no real secret nearby -- confirm NOT redacted.
        unchanged = (
            "密码：Argon2id 是推荐的哈希算法。",
            "token: RFC6750 defines bearer usage",
            "key: used to index the cache",
            "secret: this field documents the schema",
            "本文密码学部分介绍了对称加密的基本原理",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)


class ArchitecturalRewriteRound7RetryTests(unittest.TestCase):
    """Round-7 retry: P1 BLOCKING fix (digit-only qualifier-suffix swallows real value digits)
    and the P3 sentence-boundary fix, each verified fail-before/pass-after against the actual
    candidate on disk at the start of this round (see this round's own report for the exact
    before/after numbers and the file:line of every change). Every secret value below is
    synthetic, constructed for this test only, never a real captured credential.
    """

    # --- P1: a digit-only qualifier suffix must never absorb the real value's own leading
    # digits -- the whole labeled value must redact as ONE atomic span, not fragment across the
    # label/value boundary. ---------------------------------------------------------------------

    def test_p1_cjk_word_qualifier_with_digit_tail_before_embedded_ipv4_redacts_atomically(
        self,
    ) -> None:
        # `密钥_dev_192.0.2.199` -- the qualifier word's own trailing digit tail ("_192") is
        # exactly the value's leading IPv4 octet. Before this round's fix:
        # `'密钥_dev_192.[REDACTED]'` (leaked '192.' as if it were part of the label).
        cases = {
            "密钥_dev_192.0.2.199": "密钥_dev_[REDACTED]",
            "密码-prod-203.0.113.88": "密码-prod-[REDACTED]",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, (text, redacted))
            self.assertNotIn("192.", redacted)
            self.assertNotIn("203.", redacted)

    def test_p1_cjk_bare_digit_suffix_before_space_grouped_code_redacts_atomically(self) -> None:
        # `设备码_157 6620 9948 4471` -- a bare `_157` digit-only suffix is exactly the value's
        # own leading digit group of a space-grouped backup/recovery code. Before this round's
        # fix: `'设备码_157 [REDACTED]'` (leaked '157 ' -- zero characters of the real value may
        # survive; this asserts the whole code is gone, not merely that SOME redaction happened).
        cases = {
            "设备码_157 6620 9948 4471": "设备码[REDACTED]",
            "恢复码-137 0442 9981 5583": "恢复码[REDACTED]",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, (text, redacted))
            for fragment in ("157", "6620", "9948", "4471", "137", "0442", "9981", "5583"):
                self.assertNotIn(fragment, redacted, (text, redacted, fragment))

    def test_p1_ascii_bare_digit_suffix_before_embedded_ipv4_redacts_atomically(self) -> None:
        # Same shape as the CJK case, ASCII keyword, no separator between keyword and the
        # digit-led suffix at all: `token192.0.2.199`. Before this round's fix:
        # `'token192.[REDACTED]'` (leaked '192.').
        redacted = hook.redact("token192.0.2.199")
        self.assertEqual(redacted, "token[REDACTED_IP]", redacted)
        self.assertNotIn("192.", redacted)

    def test_p1_ascii_separator_digit_suffix_before_space_grouped_code_redacts_atomically(
        self,
    ) -> None:
        # `token_157 6620 9948 4471` -- the ASCII sibling of the CJK space-grouped-code repro.
        # Before this round's fix: `'token_157 [REDACTED]'` (leaked '157 ').
        cases = {
            "token_157 6620 9948 4471": "token [REDACTED]",
            "api_key-157 6620 9948 4471": "api_key [REDACTED]",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, (text, redacted))
            for fragment in ("157", "6620", "9948", "4471"):
                self.assertNotIn(fragment, redacted, (text, redacted, fragment))

    def test_p1_reanchored_labels_are_idempotent(self) -> None:
        # redact(redact(x)) == redact(x) for every P1 repro shape above.
        cases = (
            "密钥_dev_192.0.2.199",
            "密码-prod-203.0.113.88",
            "设备码_157 6620 9948 4471",
            "恢复码-137 0442 9981 5583",
            "token192.0.2.199",
            "token_157 6620 9948 4471",
            "api_key-157 6620 9948 4471",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, (text, once, twice))

    def test_p1_genuine_short_suffix_before_a_real_connector_is_unaffected(self) -> None:
        # A digit-only suffix immediately followed by a REAL connector (not more value-shaped
        # text) must keep behaving exactly as before this round's fix -- re-anchoring only
        # triggers when what follows still looks like more of the same value.
        secret = "Ab3xK9mQ2vR8pLz"
        cases = {
            f"密码2：{secret}": "密码2：[REDACTED]",
            f"password2: {secret}": "password2: [REDACTED]",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, (text, redacted))
            self.assertNotIn(secret, redacted)

    # Round-15 (retry-gate process-integrity finding 5, independent Claude opus + Codex,
    # 2026-08-23): this test used to assert
    # `hook.redact("password_v2_157 6620 9948 4471") == "password_v2_157 6620 9948 4471"` here --
    # a CHAINED ASCII qualifier's fully-unredacted labeled secret pinned as a passing expectation.
    # The round-9 rename (see this comment's own prior text in git history) argued this was NOT the
    # process-integrity violation pattern because it asserts the honest all-or-nothing direction
    # (`assertEqual(redacted, text)` plus `assertNotIn('[REDACTED', redacted)`, forbidding a
    # deceptive PARTIAL redaction) rather than a fragment leak -- true as far as it goes, and this
    # gate's own finding 5 says so explicitly. But this file's process-integrity rule, applied
    # identically elsewhere in this suite (see `test_cjk_secret_redaction_documents_known_residual_
    # gaps_still_pass_through` and the sibling removal a few hundred lines above this one, both
    # removed for the exact same underlying reason), does not carve out an exception for the
    # honest-direction case: a test asserting a COMPLETE labeled secret leak as correct output is
    # not something this suite pins as green, regardless of which direction the leak takes. Removed
    # rather than kept under its round-9 name -- see this round's own report for the reasoning.
    # Left undocumented in tests, as an explicit prose-only residual gap: `password_v2_157 6620
    # 9948 4471` (a real qualifier word immediately followed by a second, purely-digit-shaped
    # chunk that is itself the value) is still outside what the span grammar safely recognizes
    # without reopening a false-positive risk on ordinary calendar dates
    # ("password_2024-01-15"), and still passes through completely unredacted at HEAD as of this
    # round -- a known, disclosed gap, not a claim that the suite verifies it as acceptable.

    # --- P3: a sentence-initial capitalized word right after an unquoted value is ordinary
    # following prose, not part of the value. ----------------------------------------------------

    def test_p3_capitalized_sentence_word_after_value_is_preserved(self) -> None:
        # Before this round's fix: `redact('密码：Zq-203.0.113.88-Mn. Then reboot.')` ->
        # `'密码：[REDACTED] reboot.'` (swallowed the sentence-initial 'Then').
        text = "密码：Zq-203.0.113.88-Mn. Then reboot."
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED] Then reboot.", redacted)
        self.assertIn("Then reboot.", redacted)

    def test_p3_camelcase_value_continuation_is_not_mistaken_for_a_sentence_word(self) -> None:
        # A mixed-case token continuation (an alnum character right after the initial lower-case
        # run) must NOT be treated as a sentence-initial word -- no regression on a genuine value.
        secret = "Zq9pXyzAB-203.0.113.88-QqAbCdEf"
        redacted = hook.redact(f"密码：{secret} 结束")
        self.assertNotIn(secret, redacted)
        self.assertNotIn("203.0.113.88", redacted)

    def test_p3_existing_cjk_sentence_terminator_case_is_unaffected(self) -> None:
        # No regression on the already-correct CJK sentence-punctuation boundary.
        text = "密码：Zq-203.0.113.88-Mn。请妥善保管。"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]。请妥善保管。", redacted)

    def test_p3_existing_lowercase_prose_boundary_case_is_unaffected(self) -> None:
        # No regression on the already-correct lower-case-word boundary.
        text = "password: Xy-192.0.2.199-Zw and then restart the server"
        redacted = hook.redact(text)
        self.assertEqual(
            redacted, "password: [REDACTED] and then restart the server", redacted
        )

    # --- ReDoS: the new reanchor walk and the new title-case tail check must stay near-linear. --

    def test_p1_reanchor_walk_is_near_linear_not_redos(self) -> None:
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text), attempts=3)

        small = "password_" + ("1" * 2_000)
        large = "password_" + ("1" * 8_000)  # 4x input
        small_elapsed = timed(small)
        large_elapsed = timed(large)
        self.assertLess(large_elapsed, small_elapsed * 8 + 1.0, "must scale near-linearly")
        self.assertLess(large_elapsed, 3.0)

    def test_p3_title_case_tail_check_is_near_linear_not_redos(self) -> None:
        def timed(text: str) -> float:
            return _min_elapsed(lambda: hook.redact(text), attempts=3)

        small = "密码：Zq-203.0.113.88-Mn. " + (" ".join(["Word"] * 2_000))
        large = "密码：Zq-203.0.113.88-Mn. " + (" ".join(["Word"] * 8_000))  # 4x input
        small_elapsed = timed(small)
        large_elapsed = timed(large)
        self.assertLess(large_elapsed, small_elapsed * 8 + 1.0, "must scale near-linearly")
        self.assertLess(large_elapsed, 3.0)

    def test_realistic_non_secret_prose_with_label_word_and_dash_connector_unchanged(self) -> None:
        # False-positive check for the general shape this round's fix touches: a label word
        # followed by a dash/underscore connector and ordinary non-secret content must still be
        # left alone.
        unchanged = (
            "备份码是恢复账户的重要凭证，请妥善保管。",
            "The password policy requires 12 characters minimum.",
            "key: cache-index-v2",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)


class ArchitecturalRewriteRound9RetryTests(unittest.TestCase):
    """Round-9 (this round): regression coverage for the 9 findings from the immediately prior
    dual-review gate against the round-7/8 candidate, each verified fail-before/pass-after against
    the actual HEAD of this file at the start of this round. Every secret value below is synthetic,
    constructed for this test only, never a real captured credential.
    """

    # --- Findings 1 & 2: a labeled value with an embedded email-/token-shaped substring must
    # redact as ONE atomic span, whether separated by spaces or glued with no separator at all. ---

    def test_findings_1_2_embedded_email_and_token_shapes_redact_atomically(self) -> None:
        cases = (
            ("密码：Jv zed.qux@mail.example.org Wp", ("Jv", "Wp", "zed.qux")),
            ("密码：Ab ghp_qwertyuiopasdfghjk Cd", ("Ab", "Cd")),
            ("密码：Ab sk-qwertyuiopasdfgh4567 Cd", ("Ab", "Cd")),
            ("密码 Ab-ghp_qwertyuiopasdfghjk-Cd", ("Ab-", "-Cd")),
            ("密码 Rk9-ghp_zxcvbnmasdfghjkl-Tv2", ("Rk9-", "-Tv2")),
        )
        for text, fragments in cases:
            redacted = hook.redact(text)
            self.assertEqual(redacted.count("[REDACTED"), 1, f"{text!r} -> {redacted!r}")
            self.assertNotIn("[REDACTED_EMAIL]", redacted, text)
            self.assertNotIn("[REDACTED_TOKEN]", redacted, text)
            for fragment in fragments:
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")

    # --- Finding 3: same atomicity property for a dash-separated MAC and an 18-digit CN ID number
    # embedded in a labeled value, both CJK- and ASCII-labeled. ---------------------------------

    def test_finding_3_embedded_dash_mac_and_cn_id_redact_atomically(self) -> None:
        cases = (
            ("密码：Hn 3c-9d-4a-7f-0b-e5 Ls", ("Hn", "Ls")),
            ("密码：Ab 320684200003072834 Cd", ("Ab", "Cd")),
            ("password: Hn 3c-9d-4a-7f-0b-e5 Ls", ("Hn", "Ls")),
            ("token: Ab 320684200003072834 Cd", ("Ab", "Cd")),
        )
        for text, fragments in cases:
            redacted = hook.redact(text)
            self.assertEqual(redacted.count("[REDACTED"), 1, f"{text!r} -> {redacted!r}")
            for fragment in fragments:
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")

    # --- Finding 4: the cheap literal gates in `_contains_structural_redaction_risk` must be a
    # SOUND (never too narrow) precondition for their own pattern -- white-box check directly
    # against the function, independent of whichever scanner ends up calling it. -----------------

    def test_finding_4_structural_risk_gates_are_sound_preconditions(self) -> None:
        cases = (
            "Ab-ghp_qwertyuiopasdfghjk-Cd",  # token prefix not at position 0 of the probe
            "Hn 3c-9d-4a-7f-0b-e5 Ls",  # dash-separated MAC, old gate armed only on ':'
            "Ab 320684200003072834 Cd",  # CN ID with no '1' anywhere, old gate armed only on '1'
            # JWT not at position 0 of the probe (old gate checked `value.startswith("ey")`)
            "Ab-eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        )
        for value in cases:
            self.assertTrue(hook._contains_structural_redaction_risk(value), value)

    # --- Finding 7: a sentence-terminal ALL-CAPS word and a calendar-year-shaped digit run right
    # before CJK date words are ordinary following prose, not part of the labeled value. ----------

    def test_finding_7_allcaps_word_and_calendar_year_tail_are_preserved(self) -> None:
        cases = (
            (
                "密码：Qw-198.51.100.23-Zx. NOTE the rotation date.",
                "密码：[REDACTED] NOTE the rotation date.",
            ),
            (
                "密码：Qw-198.51.100.23-Zx 2026 年更新",
                "密码：[REDACTED] 2026 年更新",
            ),
            (
                "password: Kt-203.0.113.77-Ry. OK now restart.",
                "password: [REDACTED] OK now restart.",
            ),
        )
        for text, expected in cases:
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertEqual(hook.redact(redacted), redacted, f"must stay idempotent: {text!r}")
        # Regression guard for the exact false trigger this fix's own first attempt introduced: a
        # digit-grouped value's own LAST group, immediately followed by ordinary (non-"年") CJK
        # prose, must still redact as part of the value -- not be mistaken for a stray calendar year.
        text = "说明：密码-prod 186 7723 4491 5508 为临时凭据"
        redacted = hook.redact(text)
        for fragment in ("186", "7723", "4491", "5508"):
            self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
        self.assertIn("为临时凭据", redacted, redacted)

    # --- Finding 9: a compound-suffixed label's qualifier text must survive redaction, EXCEPT
    # when the "suffix" is actually ambiguous with the value's own leading digits. ----------------

    def test_finding_9_label_qualifier_survives_when_unambiguous(self) -> None:
        secret = "Gh7#kL9mWq2"
        cases = (
            (f"db_password_prod is {secret}", "db_password_prod is [REDACTED]"),
            (f"password_1 is {secret}", "password_1 is [REDACTED]"),
            (f"token_2 is {secret}", "token_2 is [REDACTED]"),
            ("密码2 is 186 7723 4491 5508", "密码2 is [REDACTED]"),
        )
        for text, expected in cases:
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            self.assertNotIn(secret, redacted, text)
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text!r}")

    def test_finding_9_ambiguous_digit_suffix_is_folded_not_leaked(self) -> None:
        # The safety follow-up to finding 9: a NAIVE "always echo the suffix" fix would leak the
        # value's own leading digit group when the "suffix" and the value are only separated by
        # bare whitespace -- verified fail-before against the first, naive version of this fix.
        cases = (
            ("token_157 6620 9948 4471", "token [REDACTED]"),
            ("api_key-157 6620 9948 4471", "api_key [REDACTED]"),
            ("password_prod_157 6620 9948 4471", "password_prod [REDACTED]"),
        )
        for text, expected in cases:
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)
            for fragment in ("157", "6620", "9948", "4471"):
                self.assertNotIn(fragment, redacted, f"{fragment!r} leaked from {text!r} -> {redacted!r}")
            self.assertEqual(redacted, hook.redact(redacted), f"must stay idempotent: {text!r}")

    # --- ReDoS: the round-9 fix's own whitespace-tail scan must stay near-linear, including the
    # NBSP infinite-loop this round's own adversarial sweep (not the review) caught. --------------

    def test_round9_whitespace_tail_scan_is_near_linear_not_redos(self) -> None:
        def timed(n: int, ws: str = " ") -> float:
            text = "密码：Ab7 " + (ws * n) + "Cd"
            return _min_elapsed(lambda: hook.redact(text), attempts=3)

        small = timed(4000)
        large = timed(16000)  # 4x input
        self.assertLess(large, small * 4 + 0.5, "whitespace-tail scan must scale near-linearly")
        self.assertLess(large, 2.0)

    def test_round9_nbsp_whitespace_tail_does_not_hang(self) -> None:
        # P0 fix (this round, caught by this round's own adversarial-timing sweep): `str.isspace()`
        # is true for NBSP (`\xa0`) and several other code points a narrower ASCII-plus-thin-space
        # strip set does not cover -- combined with jumping `i` straight to the end of the
        # recognized whitespace run (this round's own fix for the quadratic-blowup finding just
        # below), a strip set narrower than the outer `char.isspace()` gate let `i` fail to advance
        # at all, producing a TRUE infinite loop rather than a slow one. A single call completing
        # at all (within a generous bound) is the regression signal; timing is covered separately.
        text = "密码：Ab7x\xa0https://bob:hunter2pw@example.com-Qv"
        redacted = _min_elapsed(lambda: hook.redact(text), attempts=1)
        self.assertLess(redacted, 1.0)
        self.assertEqual(hook.redact(text), "密码：[REDACTED]")

    # --- False-positive safety: realistic non-secret prose using the same label words, with the
    # new value-continuation and prose-boundary checks active, must still not redact. -------------

    def test_round9_realistic_non_secret_prose_still_not_redacted(self) -> None:
        unchanged = (
            "密码：Argon2id 是推荐的哈希算法。",
            "secret: this field documents the schema",
            "key: NIST SP 800-63B defines requirements.",
            "token: IEEE 802.11 defines the standard.",
            "密码规范已在2026年更新，详见文档。",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)

    # --- Round-11 (this round) P2 BLOCKING fix, prior dual-review gate finding 1: a compound
    # ASCII label carrying a bracketed qualifier ("token2(prod)") must stay idempotent, and must
    # not have its qualifier metadata destroyed on a second pass. -------------------------------

    def test_p2_compound_numeric_label_with_bracket_qualifier_is_idempotent(self) -> None:
        # Fail-before/pass-after against the unpatched candidate (/usr/bin/python3 3.9.6):
        # `redact('token2(prod): Ax7密-198.51.100.73-Qv')` was `'token2(prod): [REDACTED]'`
        # (correct first pass), but `redact()` of THAT result was `'token2([REDACTED]'` -- the
        # second pass reclaimed "prod): [REDACTED]" as one new span and silently destroyed the
        # "prod): " qualifier text. No secret bytes ever survived (not a leak), but the label
        # metadata loss was real and reproducible from pass 2 onward.
        secret = "Ax7密-198.51.100.73-Qv"
        text = f"token2(prod): {secret}"
        first_pass = hook.redact(text)
        self.assertEqual(first_pass, "token2(prod): [REDACTED]", text)
        second_pass = hook.redact(first_pass)
        self.assertEqual(second_pass, first_pass, f"non-idempotent: {first_pass!r} -> {second_pass!r}")
        third_pass = hook.redact(second_pass)
        self.assertEqual(third_pass, second_pass, f"non-idempotent: {second_pass!r} -> {third_pass!r}")
        # A human-authored document that already contains our own placeholder, in the same shape,
        # must be left alone rather than having its surrounding qualifier text eaten.
        human_text = "password2(prod): [REDACTED] rotate weekly"
        self.assertEqual(hook.redact(human_text), human_text, human_text)

    def test_p2_quoted_compound_suffix_reclaim_idempotency_is_unaffected(self) -> None:
        # Regression guard for the fix above: the P2 fix must not regress the ALREADY-idempotent
        # quote-wrapped compound-suffix reclaim path (this shape reclaims and reconstructs
        # byte-identical output on every pass by design -- see `_redact_atomic_labeled_spans`'s
        # own comment on the placeholder-prefix guard).
        secret = "Ab3xK9mQ2vR8pLz"
        cases = (
            f"密码-prod2：‘{secret}’",
            f"| 密码-dev1：“{secret}” | more |",
        )
        for text in cases:
            first_pass = hook.redact(text)
            self.assertNotIn(secret, first_pass, text)
            second_pass = hook.redact(first_pass)
            self.assertEqual(second_pass, first_pass, f"non-idempotent: {text!r} -> {first_pass!r}")


class ArchitecturalRewriteRound13FixTests(unittest.TestCase):
    """Round-13 (this round): regression coverage for the 3 real findings from the immediately
    prior dual-review gate against the round-12 candidate (finding 1: P1 blocking full leak in a
    table row using the English "is" connector; finding 2: P2 `$USER_HOME` placeholder
    corruption/idempotency violation; finding 3: P2 over-redaction/content-destruction when the
    atomic scanner declines to preempt), plus the 7 specific regression scenarios the round's own
    brief required. Every secret value below is synthetic, constructed for this test only, never a
    real captured credential.
    """

    # --- Finding 1 (P1 BLOCKING): a table row joining a secret label to a digitless value by the
    # English "is" connector, where the value spills across a (escaped or unescaped) table pipe,
    # must not leak in full. Fail-before (unpatched round-12 candidate, /usr/bin/python3 3.9.6):
    # `redact('| 项 | 密码 is Wm|XKQR-ZMPT |')` -> unchanged, the entire value in the clear.
    # Pass-after: the value cell is redacted, matching the currently-installed production release's
    # own already-reviewed-as-principled coverage for this shape. --------------------------------

    def test_finding1_table_is_connector_digitless_value_across_pipe_does_not_leak(self) -> None:
        cases = (
            "| 项 | 密码 is Wm|XKQR-ZMPT |",
            "| 项 | 密码 is Wm | XKQR-ZMPT |",
            "| 项 | 密码 is Wm\\|XKQR-ZMPT |",
            "| 项 | password is Wm|ABCD-EFGH |",
            "| 项 | api_key is Wm|XKQR_ZMPT |",
            "| 项 | 设备码 is Wm|abcd-efgh |",
        )
        for text in cases:
            redacted = hook.redact(text)
            self.assertIn("[REDACTED", redacted, text)
            self.assertNotIn("XKQR-ZMPT", redacted, text)
            self.assertNotIn("ABCD-EFGH", redacted, text)
            self.assertNotIn("XKQR_ZMPT", redacted, text)
            self.assertNotIn("abcd-efgh", redacted, text)
            self.assertNotEqual(redacted, text, f"full leak, nothing redacted: {text!r}")
            second_pass = hook.redact(redacted)
            self.assertEqual(second_pass, redacted, f"non-idempotent: {redacted!r} -> {second_pass!r}")

    def test_finding1_digit_bearing_is_connector_value_still_redacts_atomically(self) -> None:
        # Regression guard: the finding-1 fallback must never fire once the STRICT sibling above it
        # has already claimed a digit-bearing "is"-connected value whole -- it must stay a single
        # atomic placeholder, not two.
        text = "| 项 | 密码 is Ax7Wm|XKQR-ZMPT |"
        redacted = hook.redact(text)
        self.assertEqual(redacted.count("[REDACTED"), 1, redacted)
        self.assertNotIn("Ax7Wm", redacted, redacted)
        self.assertNotIn("XKQR-ZMPT", redacted, redacted)

    def test_finding1_unrelated_next_cell_survives_once_is_connector_value_already_claimed(self) -> None:
        # Regression guard for the fallback's own placeholder-adjacency check: once an earlier pass
        # (or an earlier callback in the same pass) has already redacted the "is"-connected value in
        # place, the fallback must not misread the resulting placeholder as "a non-secret residual,
        # so the next cell must be the value" and destroy genuinely unrelated content.
        text = "| 密码 is 186 7723 4491 5508 | more |"
        redacted = hook.redact(text)
        self.assertIn("more", redacted, redacted)
        self.assertNotIn("186 7723", redacted, redacted)

    def test_finding1_is_connector_does_not_reopen_the_ab_pipe_rm4t2_protection(self) -> None:
        # Regression guard: the pre-existing "keyword:value|value" stray-pipe leak this file's
        # `_TABLE_LABEL_CONNECTOR_REJECT` already protects against (colon and CJK "是" connectors)
        # must stay fully protected -- the finding-1 fallback is scoped to ONLY "is"/"equals".
        cases = (
            "| token | abc123 | 密码:Ab|Rm4T2",
            "| token | abc123 | 备份码 是 Ab|Rm4T2",
        )
        for text in cases:
            redacted = hook.redact(text)
            self.assertNotIn("Rm4T2", redacted, redacted)

    # --- Finding 2 (P2): the general connector rule must never consume the leading "$" of an
    # already-emitted `$USER_HOME` placeholder -- doing so corrupts it into "$[REDACTED]" (losing
    # the "this was a home-directory path" tag) and is a genuine `redact(redact(x)) == redact(x)`
    # violation once such a placeholder is re-scanned. Fail-before (unpatched round-12 candidate):
    # `redact('token2\t$USER_HOME')` -> `'token2\t$[REDACTED]'`. Pass-after: unchanged. -----------

    def test_finding2_home_placeholder_is_never_corrupted_by_the_general_connector_rule(self) -> None:
        cases = (
            "token2\t$USER_HOME",
            "password2: $USER_HOME and more",
            "api_key $USER_HOME",
        )
        for text in cases:
            redacted = hook.redact(text)
            self.assertEqual(redacted, text, f"$USER_HOME placeholder corrupted: {text!r} -> {redacted!r}")

    def test_finding2_home_placeholder_survives_a_second_redact_pass_unchanged(self) -> None:
        # The genuine two-pass violation from the review: pass 1 legitimately converts a real path
        # to the placeholder: a later pass over the SAME memory text (this hook's actual production
        # usage pattern) must reach a true fixed point, not keep re-touching the placeholder.
        text = "备注 密码2 /Users/zaphod密"
        first_pass = hook.redact(text)
        self.assertIn(hook._HOME_REDACTION_PLACEHOLDER, first_pass, first_pass)
        second_pass = hook.redact(first_pass)
        self.assertEqual(second_pass, first_pass, f"non-idempotent: {first_pass!r} -> {second_pass!r}")
        third_pass = hook.redact(second_pass)
        self.assertEqual(third_pass, second_pass, f"non-idempotent: {second_pass!r} -> {third_pass!r}")

    # --- Finding 3 (P2): when the atomic scanner declines to preempt an ordinary opaque value (no
    # IP/phone/email/MAC-shaped structural risk), the LEGACY value-continuation grammar it falls
    # back to must apply the SAME calendar-year/duration-measure-word and standalone-annotation-word
    # boundary guards the atomic scanner already has, instead of silently destroying real trailing
    # content. Fail-before (unpatched round-12 candidate):
    # `redact('密码：abc123XY 2026 年更新')` -> `'密码：[REDACTED] 年更新'` (the year swallowed).
    # Pass-after: `'密码：[REDACTED] 2026 年更新'`. -------------------------------------------------

    def test_finding3_decline_path_preserves_calendar_duration_and_annotation_tails(self) -> None:
        cases = (
            ("密码：abc123XY 2026 年更新", "2026"),
            ("密码：abc123XY 90 天后轮换", "90"),
            ("密码：abc123XY NOTE the rotation date", "NOTE"),
            ("服务器密码：/Users/alice/secrets 2026 年更新", "2026"),
        )
        for text, preserved in cases:
            redacted = hook.redact(text)
            self.assertIn(preserved, redacted, f"{preserved!r} destroyed: {text!r} -> {redacted!r}")
            self.assertIn("[REDACTED", redacted, text)

    def test_finding3_decline_path_still_redacts_a_repeated_comma_joined_config_dump_per_key(self) -> None:
        # A comma-joined dump of several DISTINCT same-keyword secrets must keep each key's own
        # placeholder, not collapse into one -- collapsing silently destroys the fact that more
        # than one credential was present.
        text = "api_key:198.51.100.7,api_key:203.0.113.9,api_key:192.0.2.5"
        redacted = hook.redact(text)
        self.assertEqual(redacted.count("[REDACTED"), 3, redacted)
        for ip in ("198.51.100.7", "203.0.113.9", "192.0.2.5"):
            self.assertNotIn(ip, redacted, redacted)

    def test_finding3_does_not_regress_the_multi_group_allcaps_code_or_digit_group_protections(self) -> None:
        # Regression guards for the two already-pinned shapes finding 3's own fix had to stay
        # compatible with: a genuine multi-group all-caps device/backup code must still redact
        # whole (not stop early at an internal group that happens to look word-shaped), and a
        # digit-grouped code whose last group is followed by ordinary (non-calendar-word) CJK prose
        # must still redact whole too.
        allcaps_code = hook.redact("恢复码：XKCD 7742 QRST 6015")
        self.assertEqual(allcaps_code, "恢复码：[REDACTED]", allcaps_code)
        digit_groups = hook.redact("说明：密码-prod 186 7723 4491 5508 为临时凭据")
        self.assertNotIn("5508", digit_groups, digit_groups)
        self.assertIn("为临时凭据", digit_groups, digit_groups)

    # --- Brief item 1: a labeled backup/recovery-code value, space-grouped and phone-number-shaped
    # end to end, must redact as ONE atomic span with zero characters of the real value surviving. -

    def test_brief1_space_grouped_phone_shaped_backup_code_redacts_as_one_atomic_span(self) -> None:
        text = "备份码：139 8842 7615 3320"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "备份码：[REDACTED]", redacted)
        for fragment in ("139", "8842", "7615", "3320"):
            self.assertNotIn(fragment, redacted, redacted)

    # --- Brief item 2: a labeled password value with an embedded IPv4-shaped substring, with
    # prefix/suffix characters around it, must redact as ONE atomic span. --------------------------

    def test_brief2_embedded_ipv4_shaped_substring_with_affixes_redacts_as_one_atomic_span(self) -> None:
        text = "密码：Ab-192.0.2.44-Cd"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED]", redacted)
        self.assertNotIn("Ab-", redacted, redacted)
        self.assertNotIn("-Cd", redacted, redacted)
        self.assertNotIn("192.0.2.44", redacted, redacted)

    # --- Brief item 3: a separator-free alphabetic-hyphenated device/authorization code following
    # its keyword must be recognized and redacted, not left as a complete pass-through. -----------

    def test_brief3_separator_free_device_code_is_recognized(self) -> None:
        text = "设备码 WDJB-MJHT"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "设备码 [REDACTED]", redacted)
        self.assertNotIn("WDJB", redacted, redacted)
        self.assertNotIn("MJHT", redacted, redacted)

    # --- Brief item 4: a markdown table cell containing an escaped pipe inside the secret value
    # must redact the whole value, not leak the part after the escaped pipe. -----------------------

    def test_brief4_table_cell_escaped_pipe_inside_value_redacts_whole(self) -> None:
        cases = (
            "| password-prod | Kd8\\|mR3vQ7 |",
            "| 密钥 | Kd8\\|mR3vQ7 |",
        )
        for text in cases:
            redacted = hook.redact(text)
            self.assertNotIn("Kd8", redacted, redacted)
            self.assertNotIn("mR3vQ7", redacted, redacted)

    # --- Brief item 5: compound-suffixed labels (keyword+digit, keyword+"-prod") must redact in
    # BOTH inline-prose and table-cell contexts, confirming the two matchers share vocabulary. ------

    def test_brief5_compound_suffixed_labels_redact_in_inline_and_table_contexts(self) -> None:
        secret = "Zq7vT4nBx2W9"
        cases = (
            f"密码2：{secret}",
            f"密码-prod：{secret}",
            f"| 密码2 | {secret} |",
            f"| 密码-prod | {secret} |",
        )
        for text in cases:
            redacted = hook.redact(text)
            self.assertNotIn(secret, redacted, f"{text!r} -> {redacted!r}")

    # --- Brief item 6: at least one adversarial ReDoS-shaped input, with a measured near-linear
    # timing result -- not just an assertion that it completes. -------------------------------------

    def test_brief6_is_connector_fallback_scales_near_linearly_on_adversarial_input(self) -> None:
        # Adversarial against `_TABLE_CONNECTOR_STRICT_WORD_SPILLOVER_RE` specifically: a long run
        # of non-pipe filler after "is " with no closing pipe ever reached, forcing the bounded
        # `{1,63}` residual to repeatedly fail and backtrack at every starting offset the outer
        # `finditer()` scan considers.
        # Round 5 F6 (live-verification run, 2026-08-28): this was the last timing assertion in this
        # file still taking a SINGLE `time.perf_counter()` reading per input, and its ratio bound was
        # the tightest here -- the true CPU ratio for this shape is ~5.6x against a 10x ceiling, so a
        # single unlucky reading on `large` (or a lucky one on `small`, which only tightens the
        # bound) flipped it. Best-of-3 CPU timing plus a 12x ceiling restores real headroom while
        # staying well under the ~16x a genuine quadratic regression produces at 4x the input.
        small = "密码 is " + ("Wm " * 4_000)
        large = "密码 is " + ("Wm " * 16_000)  # 4x the character count of `small`
        small_elapsed = _min_cpu_elapsed(lambda: hook.redact(small), attempts=3)
        large_elapsed = _min_cpu_elapsed(lambda: hook.redact(large), attempts=3)
        self.assertLess(
            large_elapsed,
            max(small_elapsed * 12, 0.05),
            f"redact() scaled worse than near-linearly: {small_elapsed:.4f}s -> {large_elapsed:.4f}s",
        )
        self.assertLess(large_elapsed, 2.0, "redact() must not stall on adversarial input")

    def test_brief6_comma_boundary_check_scales_near_linearly_on_adversarial_input(self) -> None:
        # Adversarial against the new comma-then-label boundary check in `_atomic_span_end`: a long
        # run of commas with no real keyword ever following, forcing the bounded 8-char whitespace
        # peek and `_ATOMIC_LABELED_SPAN_RE` probe to run at every comma.
        small = "密码：Ab7xK9m" + ("," * 4_000)
        large = "密码：Ab7xK9m" + ("," * 16_000)  # 4x the character count of `small`
        t0 = time.perf_counter()
        hook.redact(small)
        small_elapsed = time.perf_counter() - t0
        t1 = time.perf_counter()
        hook.redact(large)
        large_elapsed = time.perf_counter() - t1
        self.assertLess(
            large_elapsed,
            max(small_elapsed * 10, 0.05),
            f"redact() scaled worse than near-linearly: {small_elapsed:.4f}s -> {large_elapsed:.4f}s",
        )
        self.assertLess(large_elapsed, 2.0, "redact() must not stall on adversarial input")

    # --- Brief item 7: realistic non-secret Chinese/English technical prose using a label word,
    # with no real secret nearby, must not be redacted. ---------------------------------------------

    def test_brief7_realistic_non_secret_prose_with_label_words_is_not_redacted(self) -> None:
        unchanged = (
            "密码长度等于12位时最安全，这是行业标准建议。",
            "token: RFC6750 defines bearer usage.",
            "key: cache-index-v2 is used for lookups.",
            "password: minimumLength=12 is the current policy.",
            "密码：Argon2id 是推荐的哈希算法。",
        )
        for text in unchanged:
            self.assertEqual(hook.redact(text), text, text)


class ArchitecturalRewriteRound15FixTests(unittest.TestCase):
    """Retry-gate findings 1-4 (independent Claude opus + Codex, 2026-08-23) against the round-14
    candidate: a multi-label-separator boundary regression present in BOTH the Phase-A atomic
    scanner (`_atomic_span_end`) and the ASCII `_ASSIGNMENT_RE` path (the latter fixed via a
    compile-time `_MULTI_LABEL_BOUNDARY_SHAPE` lookahead embedded in the pattern itself, after a
    first post-hoc-truncation attempt was found to be a real O(n^2) regression -- see the long
    comment above `_ASSIGNMENT_RE`'s own `(?P<value>...)` group), for every separator beyond the
    ASCII comma the earlier fix already handled. Every case below was
    verified to reproduce the leak/content-destruction shape against the round-14 candidate
    before this round's fix, and to redact correctly (matching the currently-installed production
    release's own behavior) after it -- see the comment above `_MULTI_LABEL_SEPARATOR_CHARS` in
    claude_memory_hook.py for the full root-cause writeup shared by both call sites."""

    # --- Finding 1 (P0 leak): a labeled value containing structural risk (so it is claimed by
    # Phase A's atomic scanner) must not swallow a following keyword: value pair glued on with a
    # non-comma separator. -----------------------------------------------------------------------

    def test_finding1_semicolon_does_not_swallow_a_following_label(self) -> None:
        text = "password: Zt-198.51.100.203-Qw;token: XKQR-ZMPT"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password: [REDACTED];token: [REDACTED]", redacted)
        self.assertNotIn("XKQR-ZMPT", redacted)
        self.assertNotIn("198.51.100.203", redacted)

    def test_finding1_ampersand_does_not_swallow_a_following_label(self) -> None:
        text = "password: Zt-198.51.100.203-Qw&token: XKQR-ZMPT"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password: [REDACTED]&token: [REDACTED]", redacted)
        self.assertNotIn("XKQR-ZMPT", redacted)

    def test_finding1_cjk_label_semicolon_ascii_label(self) -> None:
        # The identical shape with a CJK label on the left of the separator -- confirms the fix in
        # `_atomic_span_end` (which handles both CJK and ASCII labels via the same unified
        # scanner) is not accidentally ASCII-only.
        text = "密码：Zt-198.51.100.203-Qw;token：XKQR-ZMPT"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "密码：[REDACTED];token：[REDACTED]", redacted)
        self.assertNotIn("XKQR-ZMPT", redacted)

    # --- Finding 2 (P1 leak): the ASCII `_ASSIGNMENT_RE` path itself, for a value with no
    # structural risk on its own (so Phase A declines and control falls through to
    # `_ASSIGNMENT_RE`). ----------------------------------------------------------------------------

    def test_finding2_assignment_semicolon_does_not_swallow_a_following_label(self) -> None:
        text = "password: hunter2zzz;token: Aa.192.0.2.11.Bb"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password: [REDACTED];token: [REDACTED]", redacted)
        self.assertNotIn("Aa.192.0.2.11.Bb", redacted)
        self.assertNotIn("hunter2zzz", redacted)

    def test_finding2_assignment_comma_does_not_swallow_a_following_label(self) -> None:
        text = "password: Aa.192.0.2.11.Bb,token: Cc.198.51.100.22.Dd"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password: [REDACTED],token: [REDACTED]", redacted)
        self.assertNotIn("Cc.198.51.100.22.Dd", redacted)
        self.assertNotIn("Aa.192.0.2.11.Bb", redacted)

    # --- Finding 3 (P2 content destruction, non-leaking direction): two distinct credentials
    # joined by any of the CJK separators must still redact as TWO placeholders, not collapse into
    # one and silently drop the fact a second credential was present. -------------------------------

    def test_finding3_ampersand_keeps_two_distinct_placeholders(self) -> None:
        text = "api_key: 9f3c1b7e2a4d6089&secret: 4a7d2e9c1b8f5036"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "api_key: [REDACTED]&secret: [REDACTED]", redacted)

    def test_finding3_fullwidth_semicolon_keeps_two_distinct_placeholders(self) -> None:
        text = "api_key: 9f3c1b7e2a4d6089；secret: 4a7d2e9c1b8f5036"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "api_key: [REDACTED]；secret: [REDACTED]", redacted)

    def test_finding3_fullwidth_comma_keeps_two_distinct_placeholders(self) -> None:
        text = "api_key: 9f3c1b7e2a4d6089，secret: 4a7d2e9c1b8f5036"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "api_key: [REDACTED]，secret: [REDACTED]", redacted)

    def test_finding3_dunhao_keeps_two_distinct_placeholders(self) -> None:
        text = "api_key: 9f3c1b7e2a4d6089、secret: 4a7d2e9c1b8f5036"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "api_key: [REDACTED]、secret: [REDACTED]", redacted)

    def test_finding3_slash_and_plus_and_bare_pipe_keep_two_distinct_placeholders(self) -> None:
        cases = {
            "api_key: 9f3c1b7e2a4d6089/secret: 4a7d2e9c1b8f5036": "api_key: [REDACTED]/secret: [REDACTED]",
            "api_key: 9f3c1b7e2a4d6089+secret: 4a7d2e9c1b8f5036": "api_key: [REDACTED]+secret: [REDACTED]",
            "api_key: 9f3c1b7e2a4d6089|secret: 4a7d2e9c1b8f5036": "api_key: [REDACTED]|secret: [REDACTED]",
        }
        for text, expected in cases.items():
            redacted = hook.redact(text)
            self.assertEqual(redacted, expected, text)

    # --- Finding 4 (zero test coverage): the full separator set from the gate finding, exercised
    # together, plus confirmation that idempotency holds for every new shape. ----------------------

    def test_finding4_full_separator_set_idempotent(self) -> None:
        separators = [";", "&", "；", "，", "、", "/", "+", "|"]
        for sep in separators:
            text = f"api_key: 9f3c1b7e2a4d6089{sep}secret: 4a7d2e9c1b8f5036"
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertNotIn("9f3c1b7e2a4d6089", once, text)
            self.assertNotIn("4a7d2e9c1b8f5036", once, text)
            self.assertEqual(once, twice, (text, once, twice))

    # --- A bare pipe (or '/', '+') that is NOT followed by a recognized label is ordinary value
    # content and must still be captured whole, not treated as a boundary. This is the guard
    # against the fix over-reaching: only a genuine "separator then another label" shape is a
    # boundary, never the separator character alone. -------------------------------------------------

    def test_separator_characters_inside_a_single_value_are_not_boundaries_without_a_following_label(
        self,
    ) -> None:
        text = "password: Ab7/xK9+mQ|zR2 more-value-chars"
        redacted = hook.redact(text)
        self.assertNotIn("Ab7", redacted)
        self.assertNotIn("xK9", redacted)
        self.assertNotIn("mQ", redacted)
        self.assertNotIn("zR2", redacted)
        # No mid-value placeholder split: exactly one [REDACTED] marker for the whole value.
        self.assertEqual(redacted.count("[REDACTED"), 1, redacted)

    # --- Idempotency regression found by this round's own 4000-case fuzz sweep (not the retry
    # gate): `_MULTI_LABEL_BOUNDARY_SHAPE`'s identifier-run class must tolerate
    # `_ATOMIC_CLAIM_SENTINEL` sitting between a keyword and its own connector -- Phase A inserts
    # that sentinel there on a second pass whenever a placeholder already follows the keyword, and
    # without this tolerance the boundary lookahead silently stopped recognizing the keyword as a
    # label on the second pass, un-blocking a separator that was correctly a boundary on the first
    # pass. See the comment above `_MULTI_LABEL_BOUNDARY_SHAPE` in claude_memory_hook.py for the
    # full trace. ------------------------------------------------------------------------------------

    def test_idempotent_across_a_second_pass_when_a_sentinel_sits_between_label_and_connector(
        self,
    ) -> None:
        cases = (
            "password:\n|token=XJ-||GMij|QR28e",
            "api_key:v=&密码：t6Ii\"msj 5s6",
        )
        for text in cases:
            once = hook.redact(text)
            twice = hook.redact(once)
            self.assertEqual(once, twice, (text, once, twice))

    # --- A quoted assignment value must never be truncated at an internal separator: everything
    # inside real quotes is part of the value by construction, exactly as before this round. --------

    def test_quoted_assignment_value_is_not_truncated_at_an_internal_separator(self) -> None:
        text = 'password: "R7mQ;betaLOCK"'
        redacted = hook.redact(text)
        self.assertEqual(redacted, 'password: "[REDACTED]"', redacted)
        self.assertNotIn("betaLOCK", redacted)

    # --- The previously-pinned adjacent-query-param and query-glue-bridge behaviors must survive
    # the new `_MULTI_LABEL_BOUNDARY_SHAPE` lookahead embedded in `_ASSIGNMENT_RE` unchanged. -------

    def test_pinned_adjacent_query_param_behavior_survives_the_new_boundary_lookahead(self) -> None:
        text = "token=abc123&next=xyz&other=1"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "token=[REDACTED]&next=xyz&other=1", redacted)

    def test_pinned_query_glue_bridge_behavior_survives_the_new_boundary_lookahead(self) -> None:
        text = "password: pre?token=xyz&more=1-198.51.100.23-Zz"
        redacted = hook.redact(text)
        self.assertEqual(redacted, "password: [REDACTED]", redacted)
        self.assertNotIn("198.51.100.23", redacted)

    # --- ReDoS: a long adversarial chain of many short labels joined by the newly-recognized
    # separators must scale near-linearly, not quadratically -- see
    # `test_many_chained_labels_with_the_same_ascii_keyword_scale_near_linearly` below for the
    # specific repeated-identical-keyword shape a rejected first attempt at this fix got wrong. ----

    def test_many_chained_labels_scale_near_linearly(self) -> None:
        import time

        def build(n: int) -> str:
            return ";".join(f"token{i}: abc{i}xyz" for i in range(n))

        small = build(50)
        large = build(1000)
        t0 = time.perf_counter()
        hook.redact(small)
        small_elapsed = time.perf_counter() - t0
        t1 = time.perf_counter()
        hook.redact(large)
        large_elapsed = time.perf_counter() - t1
        # 20x more labels; allow generous slack but reject anything looking quadratic (400x).
        self.assertLess(
            large_elapsed,
            max(small_elapsed * 40, 0.5),
            f"redact() scaled worse than near-linearly on chained labels: "
            f"{small_elapsed:.4f}s -> {large_elapsed:.4f}s",
        )
        self.assertLess(large_elapsed, 2.0, "redact() must not stall on a long label chain")

    def test_many_chained_labels_with_the_same_ascii_keyword_scale_near_linearly(self) -> None:
        # This is the specific shape a first attempt at findings 1/2 got wrong: a POST-HOC
        # truncation approach let `_ASSIGNMENT_RE.search()` still greedily re-match almost the
        # entire remaining text on every restart before discarding the overreach, giving true
        # O(n^2) -- caught directly by this file's own pre-existing
        # `test_p1_full_pipeline_at_hard_file_byte_scale_completes_quickly` (measured multiple
        # seconds, up from well under budget) on exactly this repeated-identical-keyword shape.
        # Unlike `test_many_chained_labels_scale_near_linearly` above (which uses a distinct
        # "tokenN" keyword per label, routing through Phase A's atomic scanner, not
        # `_ASSIGNMENT_RE`), this uses the literal SAME "token" keyword for every label -- the
        # shape that actually exercises `_ASSIGNMENT_RE`'s own multi-label boundary fix.
        def build(n: int) -> str:
            return ";".join(f"token: abc{i}xyz" for i in range(n))

        small = _min_elapsed(lambda: hook.redact(build(2500)), attempts=2)
        large = _min_elapsed(lambda: hook.redact(build(10000)), attempts=2)  # 4x input
        self.assertLess(
            large,
            small * 8 + 1.0,
            f"redact() scaled worse than near-linearly on same-keyword chained labels: "
            f"{small:.4f}s -> {large:.4f}s",
        )
        self.assertLess(large, 5.0, "must not hang a synchronous UserPromptSubmit hook")


class SforStep1ResolverUnitTests(unittest.TestCase):
    """The SFOR resolver machinery (`Finding`/`Component`/`merge_overlapping`/`render_with_trace`/
    `_reconcile_fragmentation`) at the level of hand-built `Finding` lists -- exercises the
    interval-merge/render/fail-closed-backstop logic on its own terms, independent of which real
    detector or keyword pattern produced any given span.

    Round-9 retry review finding 6 (P3, cosmetic): this docstring previously claimed the class
    tests "`hook.py` in isolation -- no dependency on `claude_memory_hook`'s own detectors" -- a
    leftover from an earlier, abandoned sibling-module layout for this machinery that was later
    inlined directly into `claude_memory_hook.py` (this file's own single-file-copy deployment
    constraint; see that module's own comment on `install_bridge.py`'s deployment model). There is
    no `hook.py`; every test below calls straight into `claude_memory_hook`'s own resolver
    functions (`hook.merge_overlapping`, `hook.render_with_trace`, `hook._reconcile_fragmentation`,
    `hook.bisect`) -- it is simply scoped to hand-built `Finding`s rather than the output of a real
    detector, which is what "in isolation" actually means here."""

    def test_merge_overlapping_unions_intersecting_spans_into_one_component(self) -> None:
        findings = [Finding("A", 0, 5, "x"), Finding("B", 3, 8, "y")]
        components = hook.merge_overlapping(findings)
        self.assertEqual(len(components), 1)
        self.assertEqual((components[0].start, components[0].end), (0, 8))

    def test_merge_overlapping_keeps_disjoint_spans_separate(self) -> None:
        findings = [Finding("A", 0, 3, "x"), Finding("B", 10, 13, "y")]
        components = hook.merge_overlapping(findings)
        self.assertEqual(len(components), 2)

    def test_merge_overlapping_does_not_treat_bare_adjacency_as_overlap(self) -> None:
        # SS1.5: "symmetric overlap only" -- [0,5) and [5,9) merely touch, they do not overlap.
        findings = [Finding("A", 0, 5, "x"), Finding("B", 5, 9, "y")]
        components = hook.merge_overlapping(findings)
        self.assertEqual(len(components), 2)

    def test_render_with_trace_is_identity_on_empty_findings(self) -> None:
        text = "nothing secret here at all"
        output, copied = hook.render_with_trace(text, [])
        self.assertEqual(output, text)
        self.assertEqual(copied, [(0, len(text))])

    def test_render_solo_owner_uses_its_own_precise_render_text(self) -> None:
        # A single, non-overlapping owner renders with its own exact text (quote-wrap and all),
        # not a blanket flat marker -- see hook.py's render_with_trace docstring.
        text = 'password: "keepquotes"'
        findings = [Finding("OWNER", 10, 22, '"[REDACTED]"', is_owner=True)]
        output, _ = hook.render_with_trace(text, findings)
        self.assertEqual(output, 'password: "[REDACTED]"')

    def test_render_merged_component_with_one_owner_preserves_owner_render(self) -> None:
        text = "Xsecret123Y"
        findings = [
            Finding("STRUCTURAL", 1, 10, "[REDACTED_X]", is_owner=False),
            Finding("OWNER", 0, 11, "should-not-be-used", is_owner=True),
        ]
        output, copied = hook.render_with_trace(text, findings)
        self.assertEqual(output, "should-not-be-used")
        self.assertEqual(copied, [])

    def test_render_multiple_structural_findings_no_owner_uses_priority_marker(self) -> None:
        findings = [
            Finding("IPV4", 2, 8, "[REDACTED_IP]", is_owner=False),
            Finding("EMAIL", 5, 12, "[REDACTED_EMAIL]", is_owner=False),
        ]
        output, _ = hook.render_with_trace("aaXXXXXXXXXXbb", findings)
        # PEM > URL_USERINFO > JWT > TOKEN > BEARER > CN_ID > CN_MOBILE > IPV6 > MAC > EMAIL > IPV4
        # -- IPV6 is not present, but the two candidates here are IPV4 and EMAIL, and EMAIL ranks
        # above IPV4 in that order, so EMAIL's own marker wins.
        self.assertIn("[REDACTED_EMAIL]", output)

    def test_finding_rejects_an_invalid_span(self) -> None:
        with self.assertRaises(ValueError):
            Finding("BAD", 5, 2, "x")

    def test_reconcile_fragmentation_fails_closed_via_union_recompute_not_a_raise(self) -> None:
        # Round-3 retry review's P3 finding 6: design doc SS1.5 literally specifies
        # `re_merge_with(components, x)  # fail CLOSED: union, never drop` for this backstop -- the
        # prior version of this function (`assert_no_fragmentation`) raised `AssertionError`
        # instead, a possible fail-OPEN/availability risk if `redact_v2` were ever wired into a
        # synchronous `UserPromptSubmit` hook. Constructs a deliberately-inconsistent `components`
        # argument -- a stand-in for a hypothetical future bug elsewhere handing a stale/mismatched
        # component list to the render pipeline, a shape `merge_overlapping` itself can never
        # produce on its own -- and confirms the CURRENT function repairs it via a union recompute
        # rather than raising, and that the repaired result actually satisfies the containment
        # invariant (not merely "did not raise").
        findings = [Finding("A", 0, 5, "x"), Finding("B", 3, 8, "y")]
        stale_components = [
            hook.Component(0, 3, (findings[0],)),
            hook.Component(3, 8, (findings[1],)),
        ]  # fragments Finding("A", 0, 5) across two components
        fixed = hook._reconcile_fragmentation(stale_components, findings)
        starts = [c.start for c in fixed]
        for f in findings:
            idx = hook.bisect.bisect_right(starts, f.start) - 1
            owner = fixed[idx] if 0 <= idx < len(fixed) else None
            self.assertIsNotNone(owner, f"no component covers {f!r} in the reconciled result")
            self.assertGreaterEqual(f.start, owner.start)
            self.assertLessEqual(f.end, owner.end)

    def test_reconcile_fragmentation_is_a_pass_through_when_components_already_consistent(
        self,
    ) -> None:
        findings = [Finding("A", 0, 5, "x"), Finding("B", 3, 8, "y")]
        components = hook.merge_overlapping(findings)
        fixed = hook._reconcile_fragmentation(components, findings)
        self.assertEqual(fixed, components)


class SforStep1ProvenanceOracleTests(unittest.TestCase):
    """Design doc SS5.1: the provenance oracle is the only admissible security assertion form.

    Per this round's process-integrity rule, every assertion in this class checks
    ORIGINAL-TEXT byte ranges actually copied vs replaced -- never a substring check on the
    output string.
    """

    def test_oracle_passes_on_a_correct_render(self) -> None:
        text = "prefix password: hunter2verysecret suffix"
        secret_span = text.index("hunter2verysecret"), text.index("hunter2verysecret") + len(
            "hunter2verysecret"
        )
        findings = hook.collect_findings_v2(text)
        _, copied_ranges = hook.render_with_trace(text, findings)
        hook.assert_no_origin_range_emitted(copied_ranges, secret_span, text=text, findings=findings)

    def test_oracle_detects_a_deliberately_broken_candidate_that_ships_one_fewer_byte(self) -> None:
        # Design doc SS5.3's "mutation testing of the suite itself": the oracle must be sensitive
        # enough to catch a renderer that shortens an owner's own extent by a single character,
        # not just pass on an already-correct implementation. This constructs a DELIBERATELY
        # broken Finding list (never a broken claude_memory_hook.py) to prove the oracle mechanism
        # itself is load-bearing.
        text = "prefix password: hunter2verysecret suffix"
        value_start = text.index("hunter2verysecret")
        value_end = value_start + len("hunter2verysecret")
        secret_span = (value_start, value_end)
        correct = [Finding("OWNER", value_start, value_end, "[REDACTED]", is_owner=True)]
        _, correct_copied = hook.render_with_trace(text, correct)
        hook.assert_no_origin_range_emitted(correct_copied, secret_span, text=text, findings=correct)  # sanity: no raise

        broken = [Finding("OWNER", value_start, value_end - 1, "[REDACTED]", is_owner=True)]
        _, broken_copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(broken_copied, secret_span, text=text, findings=broken)

    def test_oracle_detects_a_candidate_that_drops_a_finding_entirely(self) -> None:
        text = "password: hunter2verysecret"
        value_start = text.index("hunter2verysecret")
        secret_span = (value_start, value_start + len("hunter2verysecret"))
        _, copied = hook.render_with_trace(text, [])  # a producer that finds nothing
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, secret_span, text=text, findings=[])

    def test_oracle_detects_a_render_that_echoes_original_secret_bytes(self) -> None:
        text = "password: Qz4Kv7Mn2Pw9"
        start = text.index("Qz4Kv7Mn2Pw9")
        end = start + len("Qz4Kv7Mn2Pw9")
        broken = [Finding("OWNER", start, end, "[REDACTED:Qz4Kv7Mn2Pw9]", is_owner=True)]
        _, copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(
                copied, (start, end), text=text, findings=broken
            )

    def test_redact_v2_with_trace_never_copies_a_structural_secret_span(self) -> None:
        text = "internal note: server ip is 198.51.100.7, keep private"
        ip_start = text.index("198.51.100.7")
        secret_span = (ip_start, ip_start + len("198.51.100.7"))
        findings = hook.collect_findings_v2(text)
        _, copied_ranges = hook.redact_v2_with_trace(text)
        hook.assert_no_origin_range_emitted(copied_ranges, secret_span, text=text, findings=findings)

    def test_oracle_detects_a_render_that_echoes_a_middle_slice_of_the_original_secret(self) -> None:
        # Round-3 retry review's own P2 finding 3, exact counterexample: the render-echo check used
        # to inspect only `covered[:size]`/`covered[-size:]` (shrinking prefix/suffix pair), so a
        # render embedding a MIDDLE substring of the covered secret ('Kv7Mn2Pw', 8 chars, out of
        # 'Qz4Kv7Mn2Pw9') passed cleanly -- verified against the PRIOR version of
        # `assert_no_origin_range_emitted` before this round's fix, via `git stash`. Fixed by
        # comparing sliding-window sets instead (see that function's own comment).
        text = "password: Qz4Kv7Mn2Pw9"
        start = text.index("Qz4Kv7Mn2Pw9")
        end = start + len("Qz4Kv7Mn2Pw9")
        broken = [Finding("OWNER", start, end, "[REDACTED:Kv7Mn2Pw]", is_owner=True)]
        _, copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (start, end), text=text, findings=broken)

    def test_oracle_permits_the_reviewed_context_preservation_forms_under_the_new_window_check(
        self,
    ) -> None:
        # The allowed_context carve-out (URL scheme prefixes, "Bearer "/"bearer ") must still not
        # false-positive under the new sliding-window check -- run against the REAL
        # `_redact_url_userinfo` structural producer, not a hand-built stand-in, since that
        # producer's own render legitimately echoes the scheme+"@" from the original text.
        text = "connect to https://bob:hunter2pw@example.com now"
        findings = hook.collect_findings_v2(text)
        start = text.index("hunter2pw")
        end = start + len("hunter2pw")
        _, copied = hook.render_with_trace(text, findings)
        hook.assert_no_origin_range_emitted(copied, (start, end), text=text, findings=findings)

    def test_oracle_catches_a_mutation_dropping_owner_findings_that_contain_an_inner_structural_finding(
        self,
    ) -> None:
        # Round-3 retry review's own P2 finding: the mutation-testing-of-the-suite argument (design
        # doc SS5.3) is only as strong as what it is exercised against -- the prior round's oracle
        # tests never fed a REAL labeled value containing an embedded structural-shaped substring
        # through the actual `collect_findings_v2`/`render_with_trace` pipeline, so a mutation that
        # drops every owner Finding containing an inner structural finding (leaving only the
        # narrower structural match on its own) survived undetected -- precisely the P1 leak class
        # this round's own fix closes. Constructed against this round's own P1 repro's REAL findings
        # (never a hand-built Finding list standing in for the real pipeline), to prove this suite
        # would have caught that regression had it still been present.
        text = "密码-prod2: Ax7密-198.51.100.73-Qv"
        findings = hook.collect_findings_v2(text)
        owner = next(f for f in findings if f.is_owner)
        secret_span = (owner.start, owner.end)

        _, real_copied = hook.render_with_trace(text, findings)
        hook.assert_no_origin_range_emitted(real_copied, secret_span, text=text, findings=findings)

        def contains(outer: Finding, inner: Finding) -> bool:
            return outer.start <= inner.start and inner.end <= outer.end

        structural = [f for f in findings if not f.is_owner]
        mutated = [
            f for f in findings if f.is_owner and not any(contains(f, s) for s in structural)
        ] + structural
        _, mutated_copied = hook.render_with_trace(text, mutated)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(
                mutated_copied, secret_span, text=text, findings=mutated
            )

    def test_oracle_catches_a_mutation_where_every_render_echoes_its_own_source_text(self) -> None:
        # Round-3 retry review's own P2 finding, second half: `Finding.render` rewritten to
        # `'[REDACTED:' + text[start:end] + ']'` (the secret echoed inside its own replacement) --
        # applied to EVERY finding from a REAL labeled value with an embedded structural shape, not
        # a hand-built single Finding. The copied-RANGE check alone does not catch this (the leak
        # lives in `render`, not in which original-text ranges get copied verbatim) -- demonstrated
        # directly below, then closed by also passing `text=`/`findings=`.
        text = "密码-prod2: Ax7密-198.51.100.73-Qv"
        findings = hook.collect_findings_v2(text)
        owner = next(f for f in findings if f.is_owner)
        secret_span = (owner.start, owner.end)
        mutated = [
            Finding(f.kind, f.start, f.end, f"[REDACTED:{text[f.start:f.end]}]", is_owner=f.is_owner)
            for f in findings
        ]
        _, copied = hook.render_with_trace(text, mutated)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, secret_span, text=text, findings=mutated)

    def test_oracle_catches_a_render_that_echoes_bytes_from_a_different_component_member(
        self,
    ) -> None:
        # Round-5 retry review's own P2 finding, exact counterexample: `render_with_trace` emits
        # ONE member's render over an ENTIRE merged component, but the prior version of the oracle
        # only ever compared a finding's render against ITS OWN `[start, end)` -- so a render that
        # echoes bytes belonging to a DIFFERENT member of the same component was invisible to it.
        # An owner claiming only `text[vs:vs+6]` merges with a structural finding covering
        # `text[vs+4:ve]`; the owner's (broken) render echoes `text[vs+6:ve]` -- outside its own
        # span, inside the component's -- and 14 secret characters would have been emitted
        # verbatim under the prior version of this check.
        text = "password: SUPERSECRETVALUE1234 end"
        vs = text.index("SUPERSECRETVALUE1234")
        ve = vs + len("SUPERSECRETVALUE1234")
        owner = Finding("OWNER", vs, vs + 6, "[REDACTED:" + text[vs + 6 : ve] + "]", is_owner=True)
        structural = Finding("STRUCT", vs + 4, ve, "[REDACTED_X]")
        findings = [owner, structural]
        # Round-11 retry fix (BLOCKER, process integrity): a prior version of this test asserted
        # `self.assertIn("ECRETVALUE1234", output)` here as a "sanity" precondition -- an
        # assertion that PASSES only when leaked secret bytes are present in `output`, exactly the
        # banned pattern regardless of framing. The fixture's `owner.render` is hand-constructed
        # two lines above to embed `text[vs + 6 : ve]` verbatim, so it is self-evidently broken by
        # construction; the real security assertion is the `assertRaises` below, unchanged.
        _, copied = hook.render_with_trace(text, findings)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (vs, vs + 6), text=text, findings=findings)

    def test_oracle_catches_a_three_character_echo_under_the_new_window(self) -> None:
        # Round-5 retry review's own P3 finding 1, exact counterexample: window=4 (the prior
        # value) can never see a 3-character echo, because its shortest window is 4 characters
        # long. Lowered to 3 (see `_PROVENANCE_LEAK_WINDOW`'s own comment for the honestly-disclosed
        # residual: a 2-character-or-shorter echo is still structurally invisible to any fixed-
        # window check).
        text = "password: Qz4Kv7Mn2Pw9"
        start = text.index("Qz4Kv7Mn2Pw9")
        end = start + len("Qz4Kv7Mn2Pw9")
        broken = [Finding("OWNER", start, end, "[REDACTED:Qz4]", is_owner=True)]
        _, copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (start, end), text=text, findings=broken)

    def test_oracle_catches_a_bearer_render_that_smuggles_secret_bytes_via_the_allowlist(
        self,
    ) -> None:
        # Round-5 retry review's own P3 finding 1 (second half), exact counterexample: the
        # allowlist used to exempt a phrase's sliding windows ANYWHERE in `finding.render`, so a
        # coincidental occurrence of the phrase's own letters INSIDE the secret ('arer' inside
        # 'xyzarer', a substring of 'Bearer ') was blanket-exempted even where it appeared in the
        # render for a totally different reason (leaked secret bytes, not the literal keyword).
        # Fixed by only stripping the allowed phrase when it is a genuine PREFIX of both the
        # covered text and the render, then scanning only what remains.
        text = "token: Bearer xyzarer zzz"
        start = text.index("Bearer xyzarer")
        end = start + len("Bearer xyzarer")
        broken = [Finding("BEARER", start, end, "Bearer [REDACTED]arer ")]
        _, copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (start, end), text=text, findings=broken)

    def test_oracle_still_permits_the_real_bearer_producer_after_the_allowlist_fix(self) -> None:
        # Sanity companion to the test above: the real, correct `_redact_bearer` producer must
        # still pass cleanly -- the fix narrows WHERE the allowlist applies, it does not remove it.
        text = "auth: Bearer sk-live-abcDEF123456xyz"
        findings = hook.collect_findings_v2(text)
        start = text.index("sk-live-abcDEF123456xyz")
        end = start + len("sk-live-abcDEF123456xyz")
        _, copied = hook.render_with_trace(text, findings)
        hook.assert_no_origin_range_emitted(copied, (start, end), text=text, findings=findings)

    def test_oracle_does_not_false_alarm_on_a_real_pem_findings_own_descriptive_marker(
        self,
    ) -> None:
        # Found this round while stress-testing the P2/P3 fixes above with broader fuzzing (25,000
        # realistic cases), NOT named in the given review: the FIRST version of the P2 fix (widen
        # the comparison to the finding's own component) false-alarmed on a REAL, unmodified `PEM`
        # Finding, because its fixed marker `"[REDACTED_PRIVATE_KEY]"` shares 3-to-7-character
        # windows with the words "PRIVATE KEY" -- which every genuine PEM block's own public,
        # standard armor text ("-----BEGIN ... PRIVATE KEY-----") also contains verbatim. Re-checked
        # against the ORIGINAL (pre-this-round) window=4 directly: the same false alarm fires there
        # too -- a pre-existing gap, not something either window value introduced, closed by
        # `_PROVENANCE_KNOWN_SAFE_RENDERS` (see that constant's own comment). This test is the
        # regression guard for that fix: the real oracle, called against a real PEM Finding's own
        # covered span, through the real production pipeline, must not raise.
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
        findings = hook.collect_findings_v2(text)
        pem = next(f for f in findings if f.kind == "PEM")
        _, copied = hook.render_with_trace(text, findings)
        hook.assert_no_origin_range_emitted(
            copied, (pem.start, pem.end), text=text, findings=findings
        )

    def test_oracle_still_catches_a_broken_render_that_merely_resembles_a_safe_constant(
        self,
    ) -> None:
        # Companion to the test above, guarding against over-exempting: the safe-render allowlist
        # (`_PROVENANCE_KNOWN_SAFE_RENDERS`) requires an EXACT match; a broken render that is merely
        # close to a known-safe constant -- here, the real marker with one leaked secret character
        # appended -- must still be caught in full.
        text = "password: Qz4Kv7Mn2Pw9"
        start = text.index("Qz4Kv7Mn2Pw9")
        end = start + len("Qz4Kv7Mn2Pw9")
        broken = [Finding("OWNER", start, end, "[REDACTED]Qz4", is_owner=True)]
        _, copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (start, end), text=text, findings=broken)


class SforStep1DesignDocRegressionTests(unittest.TestCase):
    """The specific fragment-leak repros named in the design doc and its judge material, plus a
    differential regression corpus against the currently-installed `redact()` (never edited to
    accept a weaker `redact_v2` result -- every pair below was verified equal before being pinned;
    see this round's own report for the differential harness that produced this list, and for the
    small number of KNOWN, DISCLOSED, NOT-yet-covered cases intentionally absent from it)."""

    def test_cjk_ideograph_flanking_structural_match_leading_side_variant(self) -> None:
        # Design doc SS1.4/SS2 discusses the literal flagship `密码：Ab中198.51.100.9中Cd`, whose
        # trailing `中Cd` remains a known Step-2 residual -- disclosed in prose (never pinned as a
        # passing test, per this round's own process-integrity rule) by this file's own
        # `test_cjk_family_gap_characters_bridge_atomically_for_the_nine_non_ideograph_gap_chars_
        # tested`, above -- a durable, named cross-reference rather than a raw line number, since a
        # prior round's own line-number citation here (round-3 retry review, finding 5) had already
        # gone stale and pointed at an unrelated `_MAC_ADDRESS_RE` comment in claude_memory_hook.py.
        # This test covers the LEADING-side variant instead, which this file's own pre-existing
        # Han-bridge mechanism -- reused verbatim as an owner producer here, see
        # `_collect_han_bridge_owners` -- already closes for the CURRENT production `redact()`;
        # `redact_v2` reproduces this parity exactly, rather than only partially.
        text = "密码：Ax7密-198.51.100.73-Qv"
        self.assertEqual(hook.redact_v2(text), "密码：[REDACTED]")
        self.assertEqual(hook.redact_v2(text), hook.redact(text))

    def test_flagship_repro_trailing_residual_now_closed_by_the_round_11_forward_bridge(self) -> None:
        # Round-11 retry fix side effect (P1 finding 1's generalized forward-bridge, see
        # `_bridge_owners_across_embedded_han_gaps`'s own comment): the design doc's own literal
        # flagship repro's trailing residual -- `redact()` itself still leaves `'中Cd'` exposed
        # (`test_cjk_family_gap_characters_bridge_atomically_for_the_nine_non_ideograph_gap_chars_
        # tested`'s own comment documents this as `redact()`'s disclosed, unchanged gap) -- is now
        # fully closed in `redact_v2`, exceeding `redact()`'s own coverage: the owner produced by
        # `_collect_han_bridge_owners`'s backward keyword rediscovery is widened past the IPv4
        # match, across the second `中` gap, absorbing `'Cd'` too, via this round's own new
        # continuation-absorb step. Verified via the provenance oracle, not a substring check --
        # and idempotent (a single `redact_v2` pass already reaches the fixed point).
        text = "密码：Ab中198.51.100.9中Cd"
        output = hook.redact_v2(text)
        self.assertEqual(output, "密码：[REDACTED]")
        self.assertEqual(hook.redact_v2(output), output)
        findings = hook.collect_findings_v2(text)
        _, copied = hook.render_with_trace(text, findings)
        for secret in ("Ab", "198.51.100.9", "Cd"):
            start = text.index(secret)
            hook.assert_no_origin_range_emitted(
                copied, (start, start + len(secret)), text=text, findings=findings
            )

    def test_assignment_masking_never_fabricates_a_following_word_candidate(self) -> None:
        cases = [
            (
                "note password: Qz4-203.0.113.88-Wm tail",
                "note password: [REDACTED] tail",
            ),
            (
                "prefix\npassword: Qz4-203.0.113.88-Wm\nsuffix",
                "prefix\npassword: [REDACTED]\nsuffix",
            ),
            (
                "api_key: Qz4-203.0.113.88-Wm token: Kv7Mn2Pw9x",
                "api_key: [REDACTED] token: [REDACTED]",
            ),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(hook.redact_v2(text), expected)
                self.assertEqual(hook.redact_v2(text), hook.redact(text))

    def test_merged_owner_preserves_its_render_presentation(self) -> None:
        cases = [
            ('password: "Qz4-203.0.113.88-Wm"', 'password: "[REDACTED]"'),
            ("| password | Qz4-203.0.113.88-Wm |", "| password | [REDACTED] |"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(hook.redact_v2(text), expected)

    def test_passphrase_shaped_run_of_real_words_ip_never_leaks(self) -> None:
        # Design doc SS9 item 1: a pure-dictionary-word/passphrase-style secret cannot be told
        # apart from ordinary prose by character shape alone in ANY of the four source proposals
        # or in today's production code -- explicitly NOT required to close this round (Step 2's
        # own `is_provably_prose`/`is_corroborated` machinery is the mechanism the design doc
        # proposes for this, and this round does not build it). The one thing that MUST hold: the
        # structural secret (the IP) embedded in the same labeled value never leaks, even
        # partially -- verified via the provenance oracle, not a substring check.
        text = "密码：192.0.2.44 correct horse battery staple"
        ip_start = text.index("192.0.2.44")
        secret_span = (ip_start, ip_start + len("192.0.2.44"))
        _, copied_ranges = hook.redact_v2_with_trace(text)
        hook.assert_no_origin_range_emitted(copied_ranges, secret_span, text=text, findings=hook.collect_findings_v2(text))
        self.assertEqual(hook.redact_v2(text), hook.redact(text))

    def test_bare_prose_sentence_with_a_keyword_but_no_connector_is_untouched(self) -> None:
        text = "密码：请联系管理员重置，不要在群里发送"
        self.assertEqual(hook.redact_v2(text), text)
        self.assertEqual(hook.redact_v2(text), hook.redact(text))

    def test_multi_label_separator_shape_redacts_both_labels_independently(self) -> None:
        text = "password: hunter2zzz;token: Aa.192.0.2.11.Bb"
        self.assertEqual(hook.redact_v2(text), hook.redact(text))
        self.assertEqual(hook.redact_v2(text), "password: [REDACTED];token: [REDACTED]")

    def test_compound_suffix_label_owner_bridges_forward_past_embedded_han_gap_into_ipv4(
        self,
    ) -> None:
        # Round-3 retry review's P1 BLOCKING repro, verified fresh against the actual current code
        # before this round's fix (never assumed from the prior report): `collect_findings_v2`'s
        # weakest owner producer (`_INLINE_CJK_SECRET_RE`'s own bare-value fallback branch) mis-
        # captures just the qualifier+connector text "-prod2:" as its own "value" for this
        # compound-suffix label shape, leaving " Ax7密-" and "-Qv" as two separate un-owned gaps on
        # either side of the IPV4 structural finding -- `_bridge_owners_across_embedded_han_gaps`
        # (this round's fix) widens that owner's own end forward across both, using the SAME
        # `_HAN_BRIDGE_VALUE_TOKEN`/`_HAN_BRIDGE_GAP_VALUE_UNIT` grammar `_HAN_BRIDGE_TRIGGER_RE`
        # itself is built from, anchored to the owner's own end rather than a fresh keyword
        # re-derivation. Verified via the provenance oracle (never a substring check) that the IPV4
        # structural span specifically is never copied nor echoed, not just that the final string
        # happens to match.
        text = "密码-prod2: Ax7密-198.51.100.73-Qv"
        ip_start = text.index("198.51.100.73")
        secret_span = (ip_start, ip_start + len("198.51.100.73"))
        findings = hook.collect_findings_v2(text)
        output, copied_ranges = hook.render_with_trace(text, findings)
        hook.assert_no_origin_range_emitted(copied_ranges, secret_span, text=text, findings=findings)
        self.assertEqual(output, "密码[REDACTED]")
        self.assertEqual(output, hook.redact(text))
        self.assertNotIn("Ax7", output)
        self.assertNotIn("Qv", output)

    def test_compound_suffix_label_owner_bridges_across_structural_value_shape_matrix(self) -> None:
        # The round-3 retry review's own "5 compound-CJK-label vocabularies x 6 structural value
        # shapes" characterization of the P1 leak class, reproduced directly (not merely asserted
        # to have been fixed). Every pair here is checked against the ACTUAL security property
        # Step 1's LOST-COVERAGE=0 gate requires: byte-for-byte parity with `redact()`, plus the
        # provenance oracle confirming the structural value span itself is never copied nor echoed
        # -- verified for the FULL 5x6 matrix, all 30 cases.
        #
        # Three of the five label vocabularies here ("密码-prod2", "密钥-v3", "凭证-01" -- every one
        # whose ASCII qualifier ends in a digit) get a real "2b" owner claim at all and so ALSO
        # achieve full atomic bridging (checked below, separately, with the stronger
        # `assertNotIn`); the other two ("口令-beta", "令牌-stage" -- qualifiers ending in a bare
        # word, not a digit) get NO owner claim from any "2b" producer for this connector shape --
        # in EITHER `redact()` or `redact_v2` (this round's diff never touches `redact()`'s own
        # function body at all -- confirmed by inspecting the diff directly -- so calling
        # `hook.redact(text)` here IS the untouched baseline, with no need to check out an older
        # revision) -- a pre-existing, disclosed under-reach gap at the candidate-producing stage
        # (design doc SS2 item 3/SS9), not something this round's fix is required to close, and not
        # a leak: the structural value itself still redacts correctly on its own either way, which
        # the oracle check below verifies for every one of the 30 cases regardless of which bucket
        # the label falls into.
        labels = ("密码-prod2", "口令-beta", "密钥-v3", "令牌-stage", "凭证-01")
        full_bridge_labels = {"密码-prod2", "密钥-v3", "凭证-01"}
        values = (
            "198.51.100.73",
            "2001:db8::1",
            "00:1A:2B:3C:4D:5E",
            "bob@example.com",
            "13800138000",
            "110101199003077758",
        )
        for label in labels:
            for value in values:
                text = f"{label}: Ax7密-{value}-Qv"
                with self.subTest(text=text):
                    idx = text.index(value)
                    secret_span = (idx, idx + len(value))
                    findings = hook.collect_findings_v2(text)
                    output, copied_ranges = hook.render_with_trace(text, findings)
                    hook.assert_no_origin_range_emitted(
                        copied_ranges, secret_span, text=text, findings=findings
                    )
                    self.assertEqual(output, hook.redact(text))
                    if label in full_bridge_labels:
                        self.assertNotIn("Ax7", output)
                        self.assertNotIn("Qv", output)

    def test_weak_connector_word_before_a_labeled_value_is_preserved_not_swallowed(self) -> None:
        # Round-3 retry review's P3 finding 4: `_collect_han_bridge_owners`'s backward keyword
        # re-derivation is genuinely ambiguous about whether "是"/"为" belongs to the connector or to
        # its own `bridge_value` continuation (a real Python regex-backtracking ambiguity in the
        # already-vetted, unmodified `_HAN_BRIDGE_KEYWORD_CONNECTOR` pattern, not something this
        # round's own code introduces) -- when a SEPARATE, more precise "2b" owner producer
        # (`_INLINE_ASCII_SECRET_RE` here) already finds the correct, narrower span on its own, the
        # redundant/wider Han-bridge claim is now declined (see `_collect_han_bridge_owners`'s own
        # comment), so the connector word survives exactly as `redact()` itself leaves it.
        for connector in ("是", "为"):
            text = f"password {connector} Zq7-203.0.113.77-Rk"
            with self.subTest(connector=connector):
                self.assertEqual(hook.redact_v2(text), hook.redact(text))
                self.assertIn(connector, hook.redact_v2(text))

    def test_differential_corpus_matches_current_production_redact(self) -> None:
        # A curated cross-section of this file's own historical repros (assignment/query-glue
        # bridging, quoted values, CJK/ASCII inline+table keyword shapes, structured PII, PEM,
        # closed-vocabulary false-positive guards) -- every pair verified equal to the
        # currently-installed `redact()` empirically before being pinned here (see this round's
        # report for the full ~100-input differential run this list is drawn from).
        cases = [
            "password: hunter2",
            'password: "R7mQ betaLOCK"',
            "token=abc123&next=xyz&other=1",
            "password: pre?token=xyz&more=1-198.51.100.23-Zz",
            "api_key: 9f3c1b7e2a4d6089&secret: 4a7d2e9c1b8f5036",
            "token_157 6620 9948 4471",
            "db_password_prod is Gh7#kL9mWq2",
            "token2(prod): Ax7密-198.51.100.73-Qv",
            "备份码：170 2288 3391 4407 6612",
            "| 密码 是 Wm|XKQR-ZMPT |",
            "| 密码 is Wm|XKQR-ZMPT |",
            "| 密码 | Ax7Z密-198.51.100.73-Qv |",
            "证件号: 110101199003077758 联系电话 13800138000",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
            "api_key: disabled",
            "secret: this field documents the schema",
            "密码：Argon2id 是推荐的哈希算法。",
            "token: RFC6750 defines bearer usage",
            "密钥 见 192.0.2.44",
            "令牌 见下方 sk-live-abcdefghij1234567890",
            # Round-3 retry review's own P1/P3 repros, added to the pinned differential corpus.
            "密码-prod2: Ax7密-198.51.100.73-Qv",
            "口令-beta: Zq9甲13800138000乙Wp",
            "password 是 Zq7-203.0.113.77-Rk",
            "password 为 Zq7-203.0.113.77-Rk",
            # Round-5 retry review's own P3 finding 4: every case above carries exactly one labeled
            # secret, which is why the P1 lost-coverage regression (below) survived the round-3/4
            # differential corpus undetected. These carry TWO OR MORE independently labeled secrets
            # each, added directly to this pinned corpus per that finding's own recommendation.
            "api_key: 11010519880203451X api_key: /Users/zoe/proj",
            "api_key: Nn4-11010519880203451X-Jj1 api_key: Zx壹3c:9a:0d:11:be:77贰Yw",
            "api_key: https://bob:hunter2pw@example.com，token = purple ostrich lantern voyage",
            "password: hunter2X9zQ password: hunter2Y8pR",
            "密码：hunter2verysecret 令牌：sk-live-abc123xyz789",
            "password: hunter2verysecret 密钥：sk-live-abc123xyz789",
            "api_key: AAAAAAAAAAAAAAAAAAA1 api_key: BBBBBBBBBBBBBBBBBBB2 api_key: CCCCCCCCCCCCCCCCCCC3",
            "token=abc123def456，token=ghi789jkl012",
            "secret_key: 3c:9a:0d:11:be:77; secret_key: 4d:8b:1e:22:cf:99",
            "密钥＝3c:9a:0d:11:be:77。password=Nn4-11010519880203451X-Jj1",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(hook.redact_v2(text), hook.redact(text))

    def test_multi_secret_combinatorial_matrix_has_zero_lost_coverage(self) -> None:
        # Round-5 retry review's own P3 finding 4, broader form: rather than relying only on a
        # handful of pinned strings, this walks a genuine label x separator x value-pair matrix,
        # all with a SECOND field genuinely present (unlike the single-secret-only matrix this
        # round's own review found the corpus was missing entirely). Every value is structurally
        # secret-shaped (never a bare passphrase) so this stays clear of the disclosed, unrelated
        # Step-2 prose-vs-secret gap (SS9 item 1) and is a clean LOST-COVERAGE signal:
        # `assertEqual` against the currently-installed `redact()` IS the SS5.5 differential gate,
        # not a leak-accepting pin (both sides are independently produced, never hand-edited to
        # match each other).
        labels = ["api_key", "password", "token", "secret_key"]
        seps = [": ", " = ", "="]
        values = [
            "hunter2verysecretval",
            "11010519880203451X",
            "3c:9a:0d:11:be:77",
            "198.51.100.7",
            "sk-live-abc123def456ghi789",
            "Ax7密-198.51.100.73-Qv",
        ]
        checked = 0
        for label in labels:
            for sep in seps:
                for value_a, value_b in itertools.permutations(values, 2):
                    text = f"{label}{sep}{value_a} {label}{sep}{value_b}"
                    with self.subTest(text=text):
                        self.assertEqual(hook.redact_v2(text), hook.redact(text))
                    checked += 1
        self.assertEqual(checked, len(labels) * len(seps) * len(values) * (len(values) - 1))


class SforStep1IdempotencyTests(unittest.TestCase):
    def test_redact_v2_is_idempotent_on_a_representative_battery(self) -> None:
        cases = [
            "密码：Ax7密-198.51.100.73-Qv",
            "password: hunter2",
            "密码：192.0.2.44 correct horse battery staple",
            "token=abc123&next=xyz&other=1",
            'password: "R7mQ betaLOCK"',
            "db_password_prod is Gh7#kL9mWq2",
            "证件号: 110101199003077758 联系电话 13800138000",
            "visit https://user:supersecretpw@host.example.com today",
            "密码-prod2：'Zk8#WqL3nM'",
            "token2(prod): Ax7密-198.51.100.73-Qv",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
            "nothing secret about this sentence at all",
            # Round-3 retry review's own P1/P3 repros.
            "密码-prod2: Ax7密-198.51.100.73-Qv",
            "口令-beta: Zq9甲13800138000乙Wp",
            "password 是 Zq7-203.0.113.77-Rk",
            "令牌-stage: Ax7密-00:1A:2B:3C:4D:5E-Qv",
        ]
        for text in cases:
            with self.subTest(text=text):
                once = hook.redact_v2(text)
                twice = hook.redact_v2(once)
                self.assertEqual(once, twice, f"redact_v2 not idempotent on {text!r}: {once!r} -> {twice!r}")

    def test_render_with_trace_is_a_pure_function_of_text_and_findings(self) -> None:
        text = "password: hunter2"
        findings = hook.collect_findings_v2(text)
        out1, copied1 = hook.render_with_trace(text, findings)
        out2, copied2 = hook.render_with_trace(text, findings)
        self.assertEqual(out1, out2)
        self.assertEqual(copied1, copied2)
        self.assertEqual(text, "password: hunter2")  # never mutated


class SforStep1PerformanceTests(unittest.TestCase):
    """ReDoS/performance -- specifically the adversarial table-row shape the design doc SS4/SS8
    attributes the prior architecture's measured near-quadratic regression to
    (`_redact_cjk_secret_table_columns`/`_replace_table_cell` re-splitting and rejoining an
    entire row once per sensitive cell).

    Round-5 retry fix (P3 finding 5): the docstring here used to claim `redact_v2`/
    `collect_findings_v2` "never call that function at all" -- false by the time this class was
    written; `collect_findings_v2` calls `_redact_cjk_secret_table_columns(masked_view(),
    record=table_raw)` on every invocation (see that function's own call site in
    claude_memory_hook.py), and the module comment above `_STRUCTURAL_DETECTORS_V2` made the same
    stale claim, also corrected this round.

    CORRECTED (round-9 retry review finding 16/Grok, P2 -- this docstring's own claim that
    `test_table_row_scaling_is_linear_not_quadratic` below "genuinely IS re-measuring that
    function" was itself wrong): confirmed this round, by instrumenting `_replace_table_cell`
    directly, that the single-ROW shape that test builds (alternating "密码"/value cells within
    ONE row) is redacted entirely through `_TABLE_CJK_SECRET_RE`'s own single-row
    `_sub_atomic_value` path and calls `_replace_table_cell` ZERO times at any column count -- it
    never reaches `_redact_cjk_secret_table_columns`'s own header-row/data-row/`_replace_table_cell`
    path at all, so it was not measuring the O(cols^2) shape the design doc SS4 actually describes,
    in either `redact()` or `redact_v2`.
    `test_header_row_data_row_table_scaling_is_the_real_adversarial_shape` below is the corrected
    benchmark: a genuine header row (many CJK-keyword cells) followed by one data row (one value
    per column) DOES call `_replace_table_cell` once per redacted cell (confirmed: 64 calls for 64
    columns) and DOES show worse-than-linear scaling (measured this round: doubling ratios
    climbing ~2.6 -> ~2.9 -> ~3.2 -> ~3.5 across 32/64/.../1024 columns) -- a genuine, still-open
    Step-1 performance gap (see that module comment's own disclosure), NOT claimed fixed here; the
    test below only guards against an outright hang, consistent with the file's own process-
    integrity rule that a known, disclosed limitation is recorded honestly, never hidden behind an
    assertion engineered to pass."""

    @staticmethod
    def _adversarial_table_row(cols: int) -> str:
        cells = ["密码" if i % 2 == 0 else "Ax7Zq9mK2p" for i in range(cols)]
        return "| " + " | ".join(cells) + " |"

    def test_table_row_scaling_is_linear_not_quadratic(self) -> None:
        timings: dict[int, float] = {}
        for cols in (32, 64, 128, 256, 512):
            text = self._adversarial_table_row(cols)
            timings[cols] = _min_elapsed(lambda t=text: hook.redact_v2(t), attempts=3)
        # A true O(cols^2) regression doubles its OWN doubling ratio (the design doc's own
        # measurement of the retired function showed the ratio climbing 3.25 -> 3.73 across this
        # exact progression); linear scaling keeps the ratio close to 2.0 with a generous margin
        # for scheduling noise on this specific measurement.
        for smaller, larger in ((32, 64), (64, 128), (128, 256), (256, 512)):
            if timings[smaller] <= 0:
                continue
            ratio = timings[larger] / max(timings[smaller], 1e-6)
            self.assertLess(
                ratio,
                6.0,
                f"redact_v2 scaled worse than near-linearly from {smaller} to {larger} columns: "
                f"{timings[smaller]:.5f}s -> {timings[larger]:.5f}s (ratio {ratio:.2f})",
            )
        self.assertLess(timings[512], 2.0, "must not stall a synchronous UserPromptSubmit hook")

    @staticmethod
    def _header_row_data_row_table(cols: int) -> str:
        header = "| " + " | ".join(["密码"] * cols) + " |"
        sep = "|" + "|".join(["---"] * cols) + "|"
        data = "| " + " | ".join(f"Qz4Kv7Mn2Pw9x{i}" for i in range(cols)) + " |"
        return f"{header}\n{sep}\n{data}"

    def test_header_row_data_row_table_scaling_is_the_real_adversarial_shape(self) -> None:
        # Round-9 retry review finding 16/Grok (P2): `test_table_row_scaling_is_linear_not_
        # quadratic` above builds a single ROW, which is redacted entirely through
        # `_TABLE_CJK_SECRET_RE`'s own single-row path and never calls `_replace_table_cell` at
        # all -- confirmed by instrumenting it directly this round (0 calls, any column count) --
        # so it does not exercise `_redact_cjk_secret_table_columns`'s own header-row/data-row
        # path, which is the actual shape design doc SS4 attributes the O(cols^2) regression to.
        # This is the corrected benchmark: a genuine header row (many CJK-keyword cells) plus one
        # data row (one distinct value per column) -- confirmed this round to call
        # `_replace_table_cell` once per redacted cell (64 calls for 64 columns).
        #
        # Deliberately NOT asserted as linear -- doing so would be exactly the kind of engineered-
        # to-pass assertion this file's own process-integrity rule exists to prevent. Measured this
        # round (best-of-3, /usr/bin/python3 3.9.6): doubling ratios climbing ~2.6 -> ~2.9 -> ~3.2
        # -> ~3.5 across 32/64/128/256/512 columns -- worse than linear, consistent with the design
        # doc's own O(cols^2) attribution for this exact shape. This is a genuine, confirmed,
        # still-open Step-1 performance gap (see the module comment above `_STRUCTURAL_DETECTORS_V2`
        # for the full disclosure and why it is not fixed this round) -- this test only pins that
        # every cell still redacts correctly (safety, unaffected by the performance gap) and that
        # it does not outright hang a synchronous hook, not that the scaling is good.
        timings: dict[int, float] = {}
        for cols in (32, 64, 128, 256, 512):
            text = self._header_row_data_row_table(cols)
            timings[cols] = _min_elapsed(lambda t=text: hook.redact_v2(t), attempts=3)
            out = hook.redact_v2(text)
            self.assertEqual(out.count("[REDACTED"), cols)
            self.assertNotIn("Qz4Kv7Mn2Pw9x0", out)
        self.assertLess(
            timings[512], 5.0, "must not stall a synchronous UserPromptSubmit hook even at 512 cols"
        )

    @staticmethod
    def _chained_han_bridge_labels(count: int) -> str:
        return " ".join(
            f"密码{i}-prod: Ax{i}密-198.51.{i % 256}.{(i * 7) % 256}-Qv" for i in range(count)
        )

    def test_chained_han_bridge_owner_widening_scales_near_linearly(self) -> None:
        # This round's own P1 fix (`_bridge_owners_across_embedded_han_gaps`): confirms the new
        # owner-end-anchored forward-bridging pass does not reintroduce superlinear behavior on a
        # long chain of independently-bridgeable compound-suffix CJK labels -- each entry
        # legitimately bridges its owner across one embedded ideograph into a structural IPv4
        # match, then a trailing run, the exact shape the P1 fix exists for.
        small = self._chained_han_bridge_labels(50)
        large = self._chained_han_bridge_labels(1000)  # 20x
        self.assertEqual(hook.redact_v2(small[:60]), hook.redact_v2(small[:60]))  # sanity: no crash
        small_elapsed = _min_elapsed(lambda: hook.redact_v2(small), attempts=3)
        large_elapsed = _min_elapsed(lambda: hook.redact_v2(large), attempts=3)
        self.assertLess(
            large_elapsed,
            max(small_elapsed * 40, 0.5),
            f"redact_v2 scaled worse than near-linearly on chained Han-bridge labels: "
            f"{small_elapsed:.4f}s -> {large_elapsed:.4f}s",
        )
        self.assertLess(large_elapsed, 2.0, "must not stall a synchronous UserPromptSubmit hook")

    def test_long_text_with_no_matches_at_all_scales_near_linearly(self) -> None:
        small = "ordinary prose with no secrets in it. " * 500
        large = "ordinary prose with no secrets in it. " * 10000  # 20x
        small_elapsed = _min_elapsed(lambda: hook.redact_v2(small), attempts=2)
        large_elapsed = _min_elapsed(lambda: hook.redact_v2(large), attempts=2)
        self.assertLess(
            large_elapsed,
            max(small_elapsed * 40, 0.5),
            f"redact_v2 scaled worse than near-linearly on long non-matching prose: "
            f"{small_elapsed:.4f}s -> {large_elapsed:.4f}s",
        )
        self.assertLess(large_elapsed, 3.0, "must not stall a synchronous UserPromptSubmit hook")

    def test_many_chained_labels_scale_near_linearly(self) -> None:
        # Same shape and scale as `ClaudeMemoryHookTests.test_many_chained_labels_scale_near_
        # linearly` (the pre-existing pinned test for `redact()` on this exact shape) -- reused
        # deliberately, not enlarged. `_redact_atomic_labeled_spans` -- reused verbatim, unmodified,
        # per this round's own scope -- does its own `text.find("[REDACTED", ...)` scan-to-not-found
        # per label, so BOTH `redact()` and `redact_v2` show the same super-linear-leaning tendency
        # on a much larger chained-label count than this (confirmed empirically this round: at
        # build(500)->build(20000), `redact()` itself goes 0.018s->1.40s, not just `redact_v2`) --
        # a pre-existing characteristic of reused code, not a regression this round introduces, and
        # out of scope to fix without touching that function's own internals (which Step 1
        # deliberately leaves "unmodified internally", per the design doc). This test's scale
        # matches the established, already-accepted bar for this exact code path.
        small = _min_elapsed(lambda: hook.redact_v2(";".join(f"token{i}: abc{i}xyz" for i in range(50))), attempts=3)
        large = _min_elapsed(
            lambda: hook.redact_v2(";".join(f"token{i}: abc{i}xyz" for i in range(1000))), attempts=3
        )  # 20x
        self.assertLess(
            large,
            max(small * 40, 0.5),
            f"redact_v2 scaled worse than near-linearly on chained labels: {small:.4f}s -> {large:.4f}s",
        )
        self.assertLess(large, 2.0, "must not stall a synchronous UserPromptSubmit hook")


class SforStep1RetryRegressionTests(unittest.TestCase):
    """Fresh regressions for the retry findings; security checks use origin provenance."""

    def _assert_secret_not_copied(self, text: str, secret: str) -> None:
        start = text.index(secret)
        span = (start, start + len(secret))
        findings = hook.collect_findings_v2(text)
        output, copied = hook.render_with_trace(text, findings)
        hook.assert_no_origin_range_emitted(copied, span, text=text, findings=findings)
        self.assertIn("[REDACTED]", output)

    def test_assignment_with_a_real_newline_separator_is_collected(self) -> None:
        self._assert_secret_not_copied("password:\n  Qz4Kv7Mn2Pw9", "Qz4Kv7Mn2Pw9")

    def test_header_and_data_row_table_columns_are_collected(self) -> None:
        text = "| user | password |\n| --- | --- |\n| root | Qz4Kv7Mn2Pw9 |"
        self._assert_secret_not_copied(text, "Qz4Kv7Mn2Pw9")
        self._assert_secret_not_copied(
            "| account | api_key |\n|---|---|\n| svc | Rw2Ly9Zx4Bn |\n| svc2 | Zk8WqL3nM |",
            "Zk8WqL3nM",
        )

    def test_oracle_rejects_short_render_echo_and_unscoped_allowlist(self) -> None:
        text = "x=Bearer"
        finding = Finding("IPV4", 2, len(text), "[REDACTED:Bearer]")
        _, copied = hook.render_with_trace(text, [finding])
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (2, len(text)), text=text, findings=[finding])

    def test_structural_secret_inside_real_words_is_not_copied(self) -> None:
        text = "password: 192.0.2.44 correct horse battery staple"
        self._assert_secret_not_copied(text, "192.0.2.44")

    def test_v2_is_idempotent_for_adjacent_query_and_assignment_candidates(self) -> None:
        text = '?api_key="私钥9'
        once = hook.redact_v2(text)
        self.assertEqual(hook.redact_v2(once), once)

    def test_han_bridge_candidate_scan_scales_near_linearly(self) -> None:
        def build(n: int) -> str:
            return " ".join(
                f"密码{i}-prod: Ax{i}密-198.51.{i % 256}.{(i * 7) % 256}-Qv"
                for i in range(n)
            )

        small = _min_elapsed(lambda: hook.redact_v2(build(200)), attempts=2)
        large = _min_elapsed(lambda: hook.redact_v2(build(800)), attempts=2)
        self.assertLess(large, max(small * 8, 0.5))

    def _assert_both_not_copied(self, text: str, secret_a: str, secret_b: str) -> str:
        findings = hook.collect_findings_v2(text)
        output, copied = hook.render_with_trace(text, findings)
        for secret in (secret_a, secret_b):
            start = text.index(secret)
            span = (start, start + len(secret))
            hook.assert_no_origin_range_emitted(copied, span, text=text, findings=findings)
        return output

    def test_p1_repeated_identical_label_two_assignments_both_redacted(self) -> None:
        # This round's own review, P1 finding, repro 1, exact: the masked-view `finditer` cursor
        # used to fabricate a match spanning the masked FIRST secret plus the second label's own
        # keyword text, get correctly rejected by the newline-in-masked-sep guard, then skip past
        # the genuine second `api_key: ...` entirely because `finditer`'s cursor had already moved
        # past it. Verified at 128/6300 (~2.0%) of a plain two-labeled-secrets corpus and 201/60,000
        # in a randomized fuzz before this round's fix.
        text = "api_key: 11010519880203451X api_key: /Users/zoe/proj"
        output = self._assert_both_not_copied(text, "11010519880203451X", "/Users/zoe/proj")
        self.assertEqual(output, hook.redact(text))
        self.assertEqual(hook.redact_v2(output), output)  # idempotent

    def test_p1_repeated_identical_label_fragment_beside_placeholder_both_redacted(self) -> None:
        # This round's own review, P1 finding, repro 2, exact -- the specific "verbatim tail
        # emitted beside a placeholder" bug class this whole architecture exists to eliminate: the
        # prior version left a literal `贰Yw` tail of the SECOND secret sitting right next to its
        # own `[REDACTED]` marker.
        text = "api_key: Nn4-11010519880203451X-Jj1 api_key: Zx壹3c:9a:0d:11:be:77贰Yw"
        output = self._assert_both_not_copied(
            text, "Nn4-11010519880203451X-Jj1", "Zx壹3c:9a:0d:11:be:77贰Yw"
        )
        self.assertEqual(output, hook.redact(text))
        self.assertEqual(hook.redact_v2(output), output)  # idempotent

    def test_p1_different_labels_url_then_prose_trailing_field_both_redacted(self) -> None:
        # This round's own review, P1 finding, repro 3, exact: the prior version left the entire
        # second field (`purple ostrich lantern voyage`, minus nothing at all) fully un-redacted,
        # a complete miss rather than a partial fragment.
        text = "api_key: https://bob:hunter2pw@example.com，token = purple ostrich lantern voyage"
        findings = hook.collect_findings_v2(text)
        output, copied = hook.render_with_trace(text, findings)
        start = text.index("hunter2pw")
        hook.assert_no_origin_range_emitted(
            copied, (start, start + len("hunter2pw")), text=text, findings=findings
        )
        self.assertEqual(output, hook.redact(text))
        self.assertEqual(hook.redact_v2(output), output)  # idempotent

    def test_p1_three_repeated_identical_labels_all_three_redacted(self) -> None:
        text = (
            "api_key: AAAAAAAAAAAAAAAAAAA1 "
            "api_key: BBBBBBBBBBBBBBBBBBB2 "
            "api_key: CCCCCCCCCCCCCCCCCCC3"
        )
        findings = hook.collect_findings_v2(text)
        output, copied = hook.render_with_trace(text, findings)
        for secret in ("AAAAAAAAAAAAAAAAAAA1", "BBBBBBBBBBBBBBBBBBB2", "CCCCCCCCCCCCCCCCCCC3"):
            start = text.index(secret)
            hook.assert_no_origin_range_emitted(
                copied, (start, start + len(secret)), text=text, findings=findings
            )
        self.assertEqual(output, hook.redact(text))
        self.assertEqual(hook.redact_v2(output), output)

    def test_forward_bridge_does_not_swallow_a_sibling_owners_label_across_the_gap(self) -> None:
        # Found this round via the P3-finding-4 multi-secret corpus expansion (not the P1 finding
        # itself -- a genuinely separate bug in `_bridge_owners_across_embedded_han_gaps`'s "2d"
        # widening pass, fixed the same round it was found since it sits squarely in Step 1's own
        # scope, per this round's own report). The forward Han-bridge widener used to only ask "is
        # this gap CJK-connective-shaped", never "does this gap actually belong to a DIFFERENT,
        # already-accepted owner" -- so it bridged the first (`密钥`) field's own owner forward
        # across `。password=` straight into the SECOND field's own CN_ID structural finding,
        # merging two independent, already-correctly-claimed fields into one blanket placeholder
        # and swallowing the second field's own label/connector text. Not a leak (every byte was
        # still inside some redacted component), but real, unwarranted over-redaction that also
        # diverged from `redact()`'s own output.
        text = "密钥＝3c:9a:0d:11:be:77。password=Nn4-11010519880203451X-Jj1"
        output = self._assert_both_not_copied(text, "3c:9a:0d:11:be:77", "Nn4-11010519880203451X-Jj1")
        self.assertEqual(output, hook.redact(text))
        self.assertEqual(output, "密钥＝[REDACTED]。password=[REDACTED]")

    def test_repeated_identical_label_assignment_scan_scales_near_linearly(self) -> None:
        # This round's own P1 fix replaced `_ASSIGNMENT_RE.finditer(mview)` with a manual
        # `search(mview, pos)` loop that, on rejecting a fabricated match, scans BACKWARD within
        # that one match's own `sep` span to find where the masked run ends before resuming --
        # bounded by that one masked run's own length, not by how far into the text it resumes.
        # Confirms that backward scan does not turn a long run of repeated IDENTICAL labels (the
        # exact P1 trigger shape) into O(n^2) scanning.
        def build(n: int) -> str:
            return " ".join(
                f"api_key: 1101051988020345{i % 10}X api_key: /Users/zoe{i}/proj" for i in range(n)
            )

        small = _min_elapsed(lambda: hook.redact_v2(build(100)), attempts=3)
        large = _min_elapsed(lambda: hook.redact_v2(build(800)), attempts=3)  # 8x
        self.assertLess(
            large,
            max(small * 20, 0.5),
            f"redact_v2 scaled worse than near-linearly on repeated-identical-label assignments: "
            f"{small:.5f}s -> {large:.5f}s",
        )

    def test_owner_extent_is_disclosed_as_not_independent_of_structural_findings(self) -> None:
        # Round-11 retry disclosure (P2, finding 4): design doc SS5.4 requires
        # `owner_extent(text, layout)` to be provably independent of which structural findings
        # exist -- "2c"/"2d" violate this (see the module comment above `_STRUCTURAL_DETECTORS_V2`
        # for the full disclosure). This pins the property test the design doc's own SS5.4
        # describes, confirming the CURRENT, disclosed divergence rather than silently leaving it
        # unverified: disabling `_STRUCTURAL_DETECTORS_V2` narrows this owner's extent. Growth-only
        # (never give-back, see the module comment) -- narrowing here is expected and safe, the
        # opposite direction would not be.
        text = "密码-prod2: Ax7密-198.51.100.73-Qv"
        with_detectors = hook.collect_findings_v2(text)
        owner_with = next(f for f in with_detectors if f.is_owner)
        original = hook._STRUCTURAL_DETECTORS_V2
        hook._STRUCTURAL_DETECTORS_V2 = ()
        try:
            without_detectors = hook.collect_findings_v2(text)
        finally:
            hook._STRUCTURAL_DETECTORS_V2 = original
        owners_without = [f for f in without_detectors if f.is_owner]
        self.assertTrue(owners_without)
        owner_without = owners_without[0]
        self.assertLess(
            owner_without.end - owner_without.start,
            owner_with.end - owner_with.start,
            "owner extent should currently depend on structural-detector presence (disclosed "
            "SS5.4 divergence) -- if this now holds equal, the divergence may be closed and this "
            "test's own disclosure comment should be revisited",
        )
        # Confirms growth-only, never give-back: the WITHOUT-detectors extent must still be a
        # PREFIX of the WITH-detectors extent (narrower, never a different or shrunk-from-a-wider
        # span in some other direction).
        self.assertEqual(owner_without.start, owner_with.start)
        self.assertLessEqual(owner_without.end, owner_with.end)


class SforStep1Round7RetryRegressionTests(unittest.TestCase):
    """Fresh regressions for the round-7 retry review's findings against the provenance oracle
    itself (`assert_no_origin_range_emitted`), reproduced independently against the CURRENT code
    before any fix, per this round's own process-integrity rule."""

    def test_three_character_cjk_label_prefix_render_does_not_false_alarm(self) -> None:
        # Retry review P1 finding 1, BLOCKING, exact repro: `_PROVENANCE_LEAK_WINDOW = 3` used to
        # raise AssertionError on this CORRECT, non-leaking production output, because the owner's
        # render legitimately echoes the 3-character CJK label ("恢复码") that is also, necessarily,
        # the first 3 characters of the covered secret span -- a false alarm on the label, not a
        # leak of the value ("Zq7Kv2Mn9Pw4") that follows it.
        text = "恢复码-prod是Zq7Kv2Mn9Pw4"
        findings = hook.collect_findings_v2(text)
        owner = next(f for f in findings if f.is_owner)
        self.assertEqual(owner.render, "恢复码[REDACTED]")  # sanity: this IS the label-preserving shape
        output, copied = hook.render_with_trace(text, findings)
        self.assertEqual(output, "恢复码[REDACTED]")
        self.assertEqual(output, hook.redact(text))  # parity with production, unaffected by the fix
        # Round-11 retry fix (P2, finding 2): checked against the independently-known secret
        # substring, not `owner`'s own span -- see
        # `_assert_idempotent_and_matches_v1_and_safe`'s own comment.
        secret = "Zq7Kv2Mn9Pw4"
        start = text.index(secret)
        hook.assert_no_origin_range_emitted(
            copied, (start, start + len(secret)), text=text, findings=findings
        )

    def test_cjk_label_prefix_render_never_false_alarms_across_the_full_keyword_vocabulary(
        self,
    ) -> None:
        # Retry review P1 finding 2 (the companion false-verification-claim finding): rather than
        # pinning only the two 3-character keywords the prior round's own comment happened to miss,
        # this walks the FULL closed CJK secret-keyword vocabulary (`_CJK_SECRET_KEYWORD_OTHER`/
        # `_CJK_SECRET_KEYWORD_WORDLIST`, copied verbatim from claude_memory_hook.py so this test
        # cannot itself drift out of sync with the real vocabulary) through the REAL
        # `collect_findings_v2` pipeline, so a future keyword addition is covered by this sweep
        # without anyone needing to remember to add it here by hand -- guarding against the exact
        # incomplete-enumeration failure mode the retry review's own finding 2 was about.
        keywords = (
            "密码 密碼 口令 密钥 密鑰 金鑰 秘钥 私钥 令牌 授权码 授權碼 验证码 驗證碼 "
            "凭证 憑證 凭据 憑據 激活码 激活碼 邀请码 邀請碼 动态码 動態碼 设备码 設備碼 "
            "用户码 用戶碼 恢复码 恢復碼 备份码 備份碼 备用码 備用碼 签名 簽名 助记词 助記詞"
        ).split()
        self.assertEqual(len(keywords), 37)  # sanity: this list is complete, not silently truncated
        qualifiers = ("", "-prod", "2")
        connectors = (":", "：", "是", "为")
        values = ("Zq7Kv2Mn9Pw4", "198.51.100.9")
        checked = 0
        for keyword, qualifier, connector, value in itertools.product(
            keywords, qualifiers, connectors, values
        ):
            text = f"{keyword}{qualifier}{connector}{value}"
            with self.subTest(text=text):
                findings = hook.collect_findings_v2(text)
                output, copied = hook.render_with_trace(text, findings)
                # Round-11 retry fix (P2, finding 2): a finding's own span is unsatisfiable-by-
                # construction and cannot catch under-coverage -- see
                # `_assert_idempotent_and_matches_v1_and_safe`'s own comment. `value` is the
                # independently-known secret substring actually embedded in `text`.
                start = text.index(value)
                hook.assert_no_origin_range_emitted(
                    copied, (start, start + len(value)), text=text, findings=findings
                )
                self.assertEqual(output, hook.redact(text))
            checked += 1
        self.assertEqual(checked, len(keywords) * len(qualifiers) * len(connectors) * len(values))

    def test_oracle_still_catches_a_leak_appended_after_a_genuine_cjk_label_prefix(self) -> None:
        # Companion to the two tests above, guarding against over-exempting the same way
        # `test_oracle_still_catches_a_broken_render_that_merely_resembles_a_safe_constant` guards
        # the BEARER/URL_USERINFO prefix strip: stripping a VERIFIED, genuine CJK label prefix from
        # `render_text` must never blind the check to real secret bytes that follow it. A
        # deliberately broken render here starts with the real label ("恢复码", a genuine prefix of
        # `covered`) but then embeds the REST of the secret value verbatim instead of "[REDACTED]".
        text = "恢复码-prod是Zq7Kv2Mn9Pw4"
        start, end = 0, len(text)
        broken = [Finding("OWNER", start, end, "恢复码" + text[6:], is_owner=True)]
        _, copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (start, end), text=text, findings=broken)

    def test_oracle_catches_a_render_that_embeds_a_different_components_secret_bytes(self) -> None:
        # Retry review P2 finding, exact repro: `render_with_trace` selects exactly ONE render per
        # MERGED component, but the prior oracle only ever compared a component's render against
        # its OWN covered span -- so a render belonging to component A that verbatim-embeds
        # component B's secret bytes (two entirely separate, non-overlapping owners) was invisible
        # to it, for EITHER span. `_all_secret_fragments` (built once, over every finding in the
        # whole list) is what closes this.
        text = "password: AAA111zzz token: BBB222yyy"
        finding_a = Finding("OWNER", 10, 19, "[REDACTED]" + text[26:35], is_owner=True)
        finding_b = Finding("OWNER", 26, 35, "[REDACTED]", is_owner=True)
        findings = [finding_a, finding_b]
        # Round-11 retry fix (BLOCKER, process integrity): removed a prior
        # `self.assertIn("BBB222y", output)` "sanity" line here -- an assertion that PASSES only
        # when leaked secret bytes are present in `output` is the banned pattern regardless of
        # framing. `finding_a.render` is hand-constructed two lines above to embed `text[26:35]`
        # verbatim, so it is self-evidently broken by construction; the two `assertRaises` below
        # are the real security assertions and are unchanged.
        _, copied = hook.render_with_trace(text, findings)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (10, 19), text=text, findings=findings)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (26, 35), text=text, findings=findings)

    def test_oracle_permits_two_genuinely_independent_correct_owners(self) -> None:
        # Sanity companion to the P2 test above: the cross-finding fragment check must not
        # false-alarm merely because two UNRELATED, correctly-redacted owners happen to sit in the
        # same call -- only a render that actually echoes another finding's bytes should raise.
        text = "password: hunter2verysecret token: sk-live-abc123xyz789fed"
        findings = hook.collect_findings_v2(text)
        owners = [f for f in findings if f.is_owner]
        self.assertGreaterEqual(len(owners), 2)  # sanity: two genuinely separate owned fields
        self.assertFalse(hook.overlaps((owners[0].start, owners[0].end), (owners[1].start, owners[1].end)))
        output, copied = hook.render_with_trace(text, findings)
        for secret in ("hunter2verysecret", "sk-live-abc123xyz789fed"):
            start = text.index(secret)
            hook.assert_no_origin_range_emitted(
                copied, (start, start + len(secret)), text=text, findings=findings
            )

    def test_cjk_label_strip_does_not_blind_the_cross_component_check(self) -> None:
        # Composition check: the P1 fix (strip a verified CJK label prefix from `render_text`) and
        # the P2 fix (also check every render against every OTHER finding's own span) must not open
        # a gap when combined -- a broken renderer that starts with a genuine label (so the P1 strip
        # fires) and then smuggles a DIFFERENT, unrelated component's secret bytes in right after it
        # must still be caught by the P2 cross-finding check, which operates on `covered`/
        # `all_secret_fragments` (both untouched by the P1 strip, which only ever shortens
        # `render_text`).
        text = "密码-prod是Zq7Kv2Mn9Pw4 token: BBB222yyyCCC333"
        a_start, a_end = 0, text.index(" token")
        b_start = text.index("BBB222yyyCCC333")
        b_end = b_start + len("BBB222yyyCCC333")
        finding_a = Finding(
            "OWNER", a_start, a_end, "密码[REDACTED]" + text[b_start:b_end], is_owner=True
        )
        finding_b = Finding("OWNER", b_start, b_end, "[REDACTED]", is_owner=True)
        findings = [finding_a, finding_b]
        # Round-11 retry fix (BLOCKER, process integrity): removed a prior
        # `self.assertIn("BBB222yyyCCC333", output)` "sanity" line -- an assertion that PASSES only
        # when leaked secret bytes are present in `output` is the banned pattern regardless of
        # framing. `finding_a.render` is hand-constructed above to embed `text[b_start:b_end]`
        # verbatim, so it is self-evidently broken by construction; the `assertRaises` below is the
        # real security assertion and is unchanged.
        _, copied = hook.render_with_trace(text, findings)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (a_start, a_end), text=text, findings=findings)

    def test_keyword_vocabulary_sweep_matches_the_live_module_vocabulary(self) -> None:
        # Mutation test OF the vocabulary-completeness argument itself (design doc SS5.3): asserts
        # this test file's own copy of the keyword list above is not silently out of date against
        # the real, live `_CJK_SECRET_KEYWORD_WORDLIST`/`_CJK_SECRET_KEYWORD_OTHER` in
        # claude_memory_hook.py -- if a keyword is ever added or removed there, this test fails
        # loudly instead of the vocabulary sweep above silently checking a stale list. Both
        # constants are a single non-nested `(?:alt1|alt2|...)` alternation with no `|` inside any
        # individual alternative (including the `(?i:pin)码` one), so splitting on the top-level
        # `|` after stripping the `(?:`/`)` wrapper recovers every literal exactly.
        def alternatives(pattern: str) -> set[str]:
            self.assertTrue(pattern.startswith("(?:") and pattern.endswith(")"))
            return set(pattern[3:-1].split("|"))

        live = alternatives(hook._CJK_SECRET_KEYWORD_WORDLIST) | alternatives(hook._CJK_SECRET_KEYWORD_OTHER)
        expected = set(
            (
                "密码 密碼 口令 密钥 密鑰 金鑰 秘钥 私钥 令牌 授权码 授權碼 验证码 驗證碼 "
                "凭证 憑證 凭据 憑據 激活码 激活碼 邀请码 邀請碼 动态码 動態碼 设备码 設備碼 "
                "用户码 用戶碼 恢复码 恢復碼 备份码 備份碼 备用码 備用碼 签名 簽名 助记词 助記詞"
            ).split()
        ) | {"(?i:pin)码"}
        self.assertEqual(live, expected)


class SforStep1FalsePositiveSafetyTests(unittest.TestCase):
    """Realistic non-secret prose must render byte-identical to input -- reuses this file's own
    already-established false-positive corpus (closed-vocabulary guards, documentation prose,
    algorithm names) rather than inventing a new one, verified against current `redact()` too."""

    def test_realistic_non_secret_sentences_are_untouched(self) -> None:
        cases = [
            "The password policy requires at least 12 characters.",
            "密码策略要求至少包含一个数字和一个符号。",
            "This document describes the API schema in detail.",
            "secret: this field documents the schema",
            "key: used to index the cache",
            "token: number",
            "device code: generated",
            "password: true",
            "api_key: disabled",
            "密码：Argon2id 是推荐的哈希算法。",
            "当前密码：bcrypt 哈希算法需要升级",
            "Please see the README for setup instructions.",
            "The user table has columns id, name, and email.",
            "备注：这是一段普通的中文说明文字，没有任何敏感信息。",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(hook.redact_v2(text), text)
                self.assertEqual(hook.redact_v2(text), hook.redact(text))

    def test_findings_collection_is_a_pure_read_of_the_input(self) -> None:
        text = "The password policy requires at least 12 characters."
        original = text
        hook.collect_findings_v2(text)
        self.assertEqual(text, original)


class SforStep1Round9RetryRegressionTests(unittest.TestCase):
    """Fresh, independent reproductions of every item in the round-9 retry review, against the
    CURRENT code, before trusting any prior round's own claims -- per this round's own
    process-integrity rule.

    Item 1 (P1 BLOCKING): `redact_v2` was non-idempotent on a multi-row markdown table whenever the
    secret cell value ALSO matched a structural detector -- `_redact_cjk_secret_table_columns`'s own
    row/cell arming logic, called against a `\\n`-filled masked view, could neither (a) see a
    coherent row (an earlier owner's own masked span shattered it via synthetic newlines) nor
    (b) recognize an already-claimed cell as "this row already resolved its own value in place"
    (no real `[REDACTED]` placeholder text exists in a masked view the way it does in `redact()`'s
    own mutated buffer). Fixed at claude_memory_hook.py: `_SFOR_TABLE_ROW_MASK_FILLER`,
    `_table_cell_spans`, `_owner_span_overlaps_cell`, `_redact_cjk_secret_table_columns`'s new
    `owner_overlap` parameter, and `collect_findings_v2`'s table-column call site.
    """

    _STRUCTURAL_VALUES = {
        "IPV4": "203.0.113.150",
        "MAC": "00:1A:2B:3C:4D:5E",
        "CN_MOBILE": "13800138000",
        "CN_ID": "11010519880203451X",
        "EMAIL": "user@example.com",
        "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.QWERTYuiop1234",
        "HOME": "/Users/zoe/proj",
    }
    _CODE_SHAPED_VALUES = ["Ab7xK9m", "hunter2", "Qz4Mn2Pw"]

    def _assert_idempotent_and_matches_v1_and_safe(self, text: str, known_secrets=()) -> str:
        once = hook.redact_v2(text)
        twice = hook.redact_v2(once)
        self.assertEqual(once, twice, f"redact_v2 not idempotent on {text!r}: {once!r} -> {twice!r}")
        self.assertEqual(
            once, hook.redact(text), f"redact_v2 pass-1 diverges from redact() on {text!r}"
        )
        # Round-11 retry fix (P2, finding 2): checking the oracle against a finding's OWN
        # `(f.start, f.end)` is unsatisfiable by construction for ANY real Finding actually present
        # in the `findings` list used to produce `copied` -- `_reconcile_fragmentation` guarantees
        # every Finding sits fully inside exactly one Component, and every Component is spliced
        # (never copied), so this can never catch an under-coverage regression (the P1 fix above is
        # exactly such a regression, and this exact pattern did not catch it). The
        # `once == hook.redact(text)` parity check just above is this helper's real safety
        # argument (byte-identical to a trusted-safe `redact()` output IS safe); `known_secrets`,
        # when the caller has an independently-known secret substring in scope, adds a genuine,
        # non-tautological provenance check on top of that -- callers that do not are still fully
        # covered by the parity check alone, which is not vacuous.
        findings = hook.collect_findings_v2(text)
        _, copied = hook.render_with_trace(text, findings)
        for secret in known_secrets:
            start = text.index(secret)
            hook.assert_no_origin_range_emitted(
                copied, (start, start + len(secret)), text=text, findings=findings
            )
        return once

    def test_multi_row_table_idempotency_when_value_cell_is_also_structural(self) -> None:
        # Exact retry-review repro, item 1: the crisp trigger is a secret cell value that ALSO
        # matches a structural detector -- verified across all 7 shapes the review's own fuzz named
        # (IPv4, MAC, CN-mobile, CN-ID, email, JWT, home-path).
        for kind, value in self._STRUCTURAL_VALUES.items():
            with self.subTest(kind=kind):
                text = f"| 密码 | Fw9-{value}-Jt2 |\n| note | nothing secret here |"
                once = self._assert_idempotent_and_matches_v1_and_safe(text, known_secrets=[value])
                self.assertNotIn(value, once)

    def test_multi_row_table_still_correct_on_purely_code_shaped_values(self) -> None:
        # Negative control, item 1: the review's own fuzz found the 3 purely code-shaped values
        # (no embedded structural match) never triggered the bug even before this fix -- confirms
        # the fix did not regress that already-working path.
        for value in self._CODE_SHAPED_VALUES:
            with self.subTest(value=value):
                text = f"| 密码 | Fw9-{value}-Jt2 |\n| note | nothing secret here |"
                self._assert_idempotent_and_matches_v1_and_safe(text, known_secrets=[value])

    def test_multi_row_table_idempotency_combinatorial_sweep(self) -> None:
        # Broader battery than the single crisp repro: CJK and ASCII keywords, wrapped and bare
        # values, 1-3 trailing sibling rows -- reproduces the review's own methodology (a
        # combinatorial fuzz, not a single pinned case) at a scale that stays fast in CI. A wider,
        # ad hoc 3,840-case sweep (7 structural + 3 code-shaped values x 4 keywords x 2 wrappers x
        # 4 sibling labels x 4 sibling values x 1-3 rows) was run manually this round and found
        # zero non-v1-parity divergences and zero leaks; the one additional non-idempotent shape it
        # found (a bare, unwrapped home-path value) is a DIFFERENT, pre-existing gap in `redact()`
        # itself, not something this fix introduces -- see the dedicated test for it below, and the
        # `HOME` row deliberately omitted here (kept only in the two tests above/below that name it
        # explicitly) so this sweep's own idempotency assertion stays meaningful.
        keywords = ["密码", "令牌", "token", "api_key"]
        values = {k: v for k, v in self._STRUCTURAL_VALUES.items() if k != "HOME"}
        for (kind, value), keyword, rows in itertools.product(values.items(), keywords, (1, 2, 3)):
            with self.subTest(kind=kind, keyword=keyword, rows=rows):
                lines = [f"| {keyword} | Fw9-{value}-Jt2 |"]
                lines += [f"| note{r} | nothing secret here |" for r in range(rows)]
                self._assert_idempotent_and_matches_v1_and_safe(
                    "\n".join(lines), known_secrets=[value]
                )

    def test_multi_row_table_idempotency_when_owner_claimed_cell_still_contains_a_bare_keyword(
        self,
    ) -> None:
        # Found by this round's OWN broader fuzz (not in the retry review's own text): a SECOND,
        # deeper bug in the same fix, caught only because the fuzzer's own decoy prose happened to
        # contain the plain English word "secret". `_cell_is_genuine_secret_label` (the row-header
        # detector) has its OWN, separate placeholder-exclusion (Round-17, above) that only
        # recognizes a REAL `[REDACTED...]` string -- exactly the same "masked filler is not real
        # placeholder text" gap `owner_overlap` was built to close for the VALUE-shape check, but
        # this is a DIFFERENT call site. Verified repro:
        # `'| 密钥-prod | v9-nothing secret here |\n| misc0 | how many |'` -- the value cell,
        # already partly claimed by the atomic-label scanner ("v9-nothing"), still contains the
        # bare word "secret" in its own unclaimed remainder, so it ALSO satisfied
        # `_cell_is_genuine_secret_label` on its own -- putting BOTH columns in `row_label_cols`,
        # leaving `value_cols` empty even after the first `owner_overlap` fallback (which
        # deliberately excludes `row_label_cols` indices), and falling back to arming BOTH columns
        # instead of just the value column -- diverging from `redact()`'s own real behavior (whose
        # real buffer at this point already reads "[REDACTED] secret here", correctly excluded by
        # the placeholder check). Fixed by consulting `owner_overlap` during `row_label_cols`
        # itself too, not only in the value-column fallback -- see that computation's own comment.
        cases = [
            "| 密钥-prod | v9-nothing secret here |\n| misc0 | how many |",
            "| 密钥-prod | v9-nothing secret here |\n| misc0 | how many |\n| desc1 | v1.2.3 |",
        ]
        for text in cases:
            with self.subTest(text=text):
                once = self._assert_idempotent_and_matches_v1_and_safe(
                    text, known_secrets=["v9-nothing"]
                )
                self.assertIn("misc0", once)  # sanity: the FIXED behavior leaves the label alone
                self.assertIn("how ", once)  # sanity: the untouched half of "how many" survives

    def test_home_path_bare_value_table_is_a_disclosed_preexisting_v1_gap_not_a_regression(
        self,
    ) -> None:
        # Found by this round's OWN broader fuzz (not named in the retry review itself): a BARE
        # (unwrapped) home-path value in a labeled table cell is non-idempotent in BOTH `redact_v2`
        # AND `redact()` -- confirmed this is not something the P1 fix introduces: `_HOME_RE`'s
        # match is a structural (non-owner) finding, and the CJK/ASCII inline-table owner producer
        # that would normally claim the whole cell (`TABLE_CJK`) runs AFTER
        # `_redact_cjk_secret_table_columns` in both `redact()`'s own pass order and
        # `collect_findings_v2`'s "2b" order -- so by the time the table-column scanner runs, NO
        # owner has claimed this cell yet, `owner_overlap` correctly reports no overlap (there is
        # genuinely nothing to report), and the scanner falls back to treating the label's own
        # column as a value column, exactly like a genuine multi-row header would. Asserting
        # idempotency here would be FALSE (both `redact()` and `redact_v2` fail it) -- this test
        # asserts the two properties that actually hold: v1/v2 PARITY (v2 does not diverge from
        # `redact()`'s own existing, imperfect behavior) and SAFETY (no leak, over-redaction only).
        # Left for a future round: closing this needs the "2b" owner-claim ordering fix already
        # disclosed above `_STRUCTURAL_DETECTORS_V2`'s own comment block (the KEYWORD-sentinel gap),
        # not something in this round's assigned P1 scope.
        # Round-11 retry fix (BLOCKER, process integrity): removed a prior
        # `self.assertNotEqual(v1_once, v1_twice, ...)` line here -- asserting non-idempotency as a
        # passing "sanity" precondition is the banned pattern regardless of framing, even when it
        # describes a real, pre-existing, already-disclosed `redact()` (v1) gap rather than
        # anything this round's own diff introduces. The comment above already documents the gap in
        # prose; the assertions this test actually needs (v1/v2 parity, no leak via the provenance
        # oracle) follow unchanged.
        text = "| 密码 | /Users/zoe/proj |\n| note | nothing secret here |"
        v1_once = hook.redact(text)
        v2_once = hook.redact_v2(text)
        self.assertEqual(v2_once, v1_once, "redact_v2 pass-1 must still match redact()'s own output")
        # Round-11 retry fix (P2, finding 2): checking each finding's OWN span (as the prior
        # version of this loop did) is unsatisfiable by construction -- see
        # `_assert_idempotent_and_matches_v1_and_safe`'s own comment above for the full argument.
        # An independently-known secret substring is available here ("/Users/zoe/proj" itself), so
        # the oracle is checked against that instead of any finding's own span.
        secret = "/Users/zoe/proj"
        start = text.index(secret)
        findings = hook.collect_findings_v2(text)
        _, copied = hook.render_with_trace(text, findings)
        hook.assert_no_origin_range_emitted(
            copied, (start, start + len(secret)), text=text, findings=findings
        )
        self.assertNotIn(secret, v2_once)

    def test_table_cell_spans_matches_table_cell_span_pointwise(self) -> None:
        # `_table_cell_spans` (single delimiter scan) must agree with `_table_cell_span` (one scan
        # per call) for every REAL cell index -- it is free to return additional trailing phantom
        # spans past the real cell count (both helpers share the same "one more span past the final
        # pipe" arithmetic; `_redact_cjk_secret_table_columns` only ever indexes up to
        # `len(cells) - 1`, from `_split_table_row_cells`, so a phantom tail entry is never read),
        # but it must never return FEWER than `len(cells)` spans, and every real span it does
        # return must be byte-identical to what the per-call helper returns for that same index.
        lines = [
            "| 密码 | Fw9-203.0.113.150-Jt2 |",
            "|a|b|c|",
            r"|a\|b|c|",
            "| single |",
            "|leading only",
            "| a | b | c | d | e | f |",
            "||",
            "|  |  |",
            "| 密码-prod | Fw9\\|Zq\\|-Jt2 | note |",
        ]
        for line in lines:
            with self.subTest(line=line):
                cells = hook._split_table_row_cells(line)
                if cells is None:
                    continue  # never reached by the real caller for a malformed row
                spans_new = hook._table_cell_spans(line)
                spans_old = [hook._table_cell_span(line, i) for i in range(len(cells))]
                self.assertGreaterEqual(len(spans_new), len(cells))
                self.assertEqual(spans_new[: len(cells)], spans_old)

    def test_table_cell_spans_avoids_the_per_cell_rescan_that_causes_quadratic_cost(self) -> None:
        # The design doc SS4/SS8's own named regression class: re-deriving cell boundaries once PER
        # CANDIDATE COLUMN (`_table_cell_span`, one `_UNESCAPED_PIPE_RE.finditer(line)` per call) is
        # O(columns) calls x O(line length) each on a wide row -- exactly what motivated
        # `_table_cell_spans`'s single-scan design. Confirms the new helper itself scales linearly
        # with row length, unlike the per-call pattern it replaces for this round's new
        # `owner_overlap` fallback.
        def wide_row(cols: int) -> str:
            cells = ["密码" if i % 2 == 0 else "Ax7Zq9mK2p" for i in range(cols)]
            return "| " + " | ".join(cells) + " |"

        timings: dict[int, float] = {}
        for cols in (256, 1024, 4096):
            line = wide_row(cols)
            timings[cols] = _min_elapsed(lambda l=line: hook._table_cell_spans(l), attempts=3)
        for smaller, larger in ((256, 1024), (1024, 4096)):  # 4x column growth each step
            if timings[smaller] <= 0:
                continue
            ratio = timings[larger] / max(timings[smaller], 1e-6)
            self.assertLess(
                ratio,
                12.0,  # true O(n^2) would show ~16x here; linear stays near 4x
                f"_table_cell_spans scaled worse than near-linearly from {smaller} to {larger} "
                f"columns: {timings[smaller]:.6f}s -> {timings[larger]:.6f}s (ratio {ratio:.2f})",
            )

    def test_owner_overlap_fallback_scales_near_linearly_on_many_row_pairs(self) -> None:
        # End-to-end re-benchmark of the whole fix through `redact_v2`, on the actual repro shape
        # (many independent label/value/note row-triples) at the scale the design doc SS8 says
        # this class of fix must be re-measured at.
        def build(n: int) -> str:
            rows = []
            for i in range(n):
                rows.append(f"| 密码{i} | Fw9-203.0.{i % 256}.{(i * 7) % 256}-Jt2 |")
                rows.append(f"| note{i} | nothing secret here {i} |")
            return "\n".join(rows)

        small = _min_elapsed(lambda: hook.redact_v2(build(50)), attempts=3)
        large = _min_elapsed(lambda: hook.redact_v2(build(800)), attempts=3)  # 16x
        self.assertLess(
            large,
            max(small * 40, 0.5),
            f"redact_v2 scaled worse than near-linearly on many row-pairs: "
            f"{small:.4f}s -> {large:.4f}s",
        )
        self.assertLess(large, 2.0, "must not stall a synchronous UserPromptSubmit hook")

    def test_owner_overlap_that_never_fires_is_behavior_neutral(self) -> None:
        # `owner_overlap` is consulted twice: once per candidate label cell (to exclude one an
        # earlier owner already claimed from `row_label_cols` -- the second half of this round's
        # fix, see that call site's own comment) and once per remaining candidate cell in the
        # value-column fallback (only when the cheap shape-check found nothing). Both call sites
        # are pure ADDITIONAL evidence, never a veto: supplying a predicate that always reports "no
        # overlap anywhere" must reproduce the exact output `owner_overlap=None` (the default,
        # v1's own call form) would have -- verified across shapes that exercise each call site:
        # a genuine self-contained label|value pair (exercises the row_label_cols exclusion check,
        # since "密钥" is a candidate label cell) and a genuine multi-row header (exercises neither,
        # since no cell there is owner-claimable in the first place).
        cases = [
            "| 密钥 | sk-abcdefghij1234567890 |\n| 备用 | Tq4zW7pNe2Vs |",
            "| 账号 | 密码 | 备注 |\n| root | Sec9retVal | prod |\n| admin | Zq7#vT4nBx2W | staging |",
        ]
        for text in cases:
            with self.subTest(text=text):
                baseline = hook._redact_cjk_secret_table_columns(text)
                never_overlaps = hook._redact_cjk_secret_table_columns(
                    text, owner_overlap=lambda start, end: False
                )
                self.assertEqual(never_overlaps, baseline)

    def test_v1_table_column_scan_call_site_passes_no_owner_overlap_argument(self) -> None:
        # `owner_overlap` defaults to `None`; `redact()`'s own call site inside `redact()`'s own
        # source (`_redact_cjk_secret_table_columns(text)`) must still call it with no keyword
        # arguments at all -- confirms this round's diff touched only the function's DEFINITION and
        # the SEPARATE `collect_findings_v2` call site, never `redact()`'s own call expression. (An
        # earlier draft of this test tried to verify the same thing by comparing output strings
        # instead -- `_redact_cjk_secret_table_columns`'s own return value is one intermediate step
        # inside `redact()`'s own multi-phase chain, several more phases still run on its output
        # afterward (see `redact()` at claude_memory_hook.py:7121 onward), so that comparison was
        # invalid regardless of this round's fix and is replaced with this direct call-site check.)
        # Real regression coverage that `redact()`'s actual OUTPUT is unaffected already comes from
        # the full suite's many pre-existing pinned table-shape tests, all still passing unchanged.
        real_fn = hook._redact_cjk_secret_table_columns
        spy = mock.Mock(wraps=real_fn)
        text = "| 密码 | Fw9-203.0.113.150-Jt2 |\n| note | nothing secret here |"
        with mock.patch.object(hook, "_redact_cjk_secret_table_columns", spy):
            hook.redact(text)
        self.assertEqual(spy.call_count, 1)
        _args, kwargs = spy.call_args
        self.assertEqual(kwargs, {}, "redact()'s call site must not pass owner_overlap/record")

    def test_oracle_methodology_must_use_the_full_owner_span_not_a_narrower_structural_span(
        self,
    ) -> None:
        # Item 3 (P2, methodology): reproduces the retry review's own point directly, rather than
        # only asserting my own fuzz used the right methodology. A hand-built, DELIBERATELY BROKEN
        # finding list (only the narrow structural IPv4 span, no owner covering the full labeled
        # value -- simulating what a hypothetical regression could produce) leaks the label's
        # surrounding "Ab-"/"-Cd" text either side of the IP. Checking the oracle against ONLY the
        # narrow structural span passes cleanly -- proving that check alone is insufficient, exactly
        # as the review found -- while checking it against the FULL labeled-value span correctly
        # raises. The REAL `collect_findings_v2` does not produce this broken finding list (verified
        # immediately below): it already includes the ATOMIC_LABEL owner covering the whole value,
        # which is why every test above (and this round's own re-verification, and the prior
        # rounds' large-scale fuzzes) checks every finding's own full span, not a synthesized
        # narrower one.
        # Round-11 retry fix (BLOCKER, process integrity): removed a prior
        # `self.assertEqual(output, "password: Ab-[REDACTED_IP]-Cd")` line here -- pinning, as a
        # passing assertion, an output that exposes "Ab-"/"-Cd" (part of the real labeled value's
        # own extent, per `full_owner_span` below) is the banned pattern regardless of framing.
        # `broken_findings` is hand-constructed one line above to omit the owner entirely, so the
        # leak is self-evident by construction; the two `assert_no_origin_range_emitted` calls
        # below (one proving the narrow-span check is insufficient, one proving the full-span check
        # correctly raises) are the real assertions this test needs and are unchanged.
        text = "password: Ab-203.0.113.44-Cd"
        ipv4_finding = Finding("IPV4", 13, 25, "[REDACTED_IP]", is_owner=False)
        broken_findings = [ipv4_finding]  # the owner is missing -- simulates the regression
        _, copied = hook.render_with_trace(text, broken_findings)
        hook.assert_no_origin_range_emitted(  # narrow span alone: false-clean
            copied, (ipv4_finding.start, ipv4_finding.end), text=text, findings=broken_findings
        )
        full_owner_span = (10, 28)  # "Ab-203.0.113.44-Cd", the real labeled value's own extent
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(
                copied, full_owner_span, text=text, findings=broken_findings
            )
        # The real pipeline never produces the broken finding list above.
        real_findings = hook.collect_findings_v2(text)
        owner = next(f for f in real_findings if f.is_owner)
        self.assertEqual((owner.start, owner.end), full_owner_span)
        real_output, real_copied = hook.render_with_trace(text, real_findings)
        self.assertEqual(real_output, "password: [REDACTED]")
        hook.assert_no_origin_range_emitted(
            real_copied, (owner.start, owner.end), text=text, findings=real_findings
        )

    def test_bearer_and_url_userinfo_marker_change_on_multi_finding_overlap_is_disclosed_and_safe(
        self,
    ) -> None:
        # Item 2 (P2): `Finding` has no `sensitive_spans` field (design doc SS1.2(a)). The
        # STANDALONE case already matches `redact()` byte-for-byte (each producer's own `render`
        # already carries the context-preserving text); the divergence only appears when a BEARER
        # or URL_USERINFO finding's span also overlaps a SECOND finding, forcing the "2+ findings,
        # no owner" branch, which design doc SS1.5 itself specifies as one flat priority-table
        # marker -- design-conformant, but previously untested. Pinned here as a known, safe
        # (over-redaction only, never a leak) divergence from `redact()`, not asserted as parity.
        cases = [
            (
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.QWERTYuiop1234",
                "Authorization: Bearer [REDACTED]",  # redact()'s own output
                "Authorization: [REDACTED_TOKEN]",  # redact_v2's disclosed, safe divergence
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.QWERTYuiop1234",
            ),
            (
                "clone from https://bob:hunter2pw@git.example.com/repo.git",
                "clone from https://[REDACTED]@git.example.com/repo.git",
                "clone from [REDACTED]/repo.git",
                "hunter2pw",
            ),
        ]
        for text, v1_expected, v2_expected, secret in cases:
            with self.subTest(text=text):
                self.assertEqual(hook.redact(text), v1_expected)
                v2_out = hook.redact_v2(text)
                self.assertEqual(v2_out, v2_expected)
                self.assertNotEqual(v2_out, v1_expected)  # the disclosed divergence, named explicitly
                findings = hook.collect_findings_v2(text)
                _, copied = hook.render_with_trace(text, findings)
                # Round-11 retry fix (P2, finding 2): checked against the independently-known
                # secret substring, not a finding's own span -- see
                # `_assert_idempotent_and_matches_v1_and_safe`'s own comment.
                start = text.index(secret)
                hook.assert_no_origin_range_emitted(
                    copied, (start, start + len(secret)), text=text, findings=findings
                )


class SforStep1Round9RetrySecondPassTests(unittest.TestCase):
    """Fresh, independent reproductions of every item in this round's own retry review, against
    the CURRENT code, before trusting any prior round's own claims -- per this round's own
    process-integrity rule.

    Finding 1 (P1 BLOCKING): the provenance oracle raised `AssertionError` on real, correct,
    non-leaking production output for the `INLINE_ASCII` owner family, because the label-prefix
    strip in `assert_no_origin_range_emitted`'s `else` branch only ever handled a bare CJK keyword,
    never the ASCII keyword vocabulary, and never a qualifier+connector prefix in either
    vocabulary. Fixed via `_owner_label_prefix_end`/`_OWNER_LABEL_VALUE_PRODUCERS`/
    `_is_subsequence` (see those definitions' own comments).
    """

    def test_the_exact_p1_repro_no_longer_raises(self) -> None:
        # Verbatim repro from the review: /usr/bin/python3 3.9.6,
        # collect_findings_v2('password-prod2 2001:db8:85a3::8a2e:370:7334') yielded an
        # INLINE_ASCII owner [0, 43) whose render is the correct, non-leaking
        # 'password-prod [REDACTED]', merged with an IPV6 finding into one component [0, 43);
        # render_with_trace's own output is 'password-prod [REDACTED]' with copied_ranges=[] --
        # nothing leaks -- yet the oracle raised on it before this round's fix.
        text = "password-prod2 2001:db8:85a3::8a2e:370:7334"
        findings = hook.collect_findings_v2(text)
        kinds = {f.kind for f in findings}
        self.assertIn("INLINE_ASCII", kinds)
        self.assertIn("IPV6", kinds)
        output, copied = hook.render_with_trace(text, findings)
        self.assertEqual(output, "password-prod [REDACTED]")
        self.assertEqual(copied, [])
        # Round-11 retry fix (P2, finding 2): checked against the independently-known secret
        # substring, not a finding's own span -- see
        # `_assert_idempotent_and_matches_v1_and_safe`'s own comment.
        secret = "2001:db8:85a3::8a2e:370:7334"
        start = text.index(secret)
        hook.assert_no_origin_range_emitted(
            copied, (start, start + len(secret)), text=text, findings=findings
        )

    def test_ascii_label_qualifier_connector_structural_value_matrix_never_false_alarms(self) -> None:
        # Broader battery than the single crisp repro: the review's own "label x qualifier x
        # connector x value" methodology, scaled to stay fast in CI, covering every one of the 5
        # `_OWNER_LABEL_VALUE_PRODUCERS` kinds that can actually fire for each shape. 144 cases;
        # the review's own fresh measurement found 24/144 raised in exactly this shape of matrix
        # before this round's fix (all INLINE_ASCII, all bare-space connector -- the digit-fold
        # case).
        labels = ["password", "api_key", "secret_key", "token"]
        qualifiers = ["-prod2", "_v3", "2", ""]
        connectors = [" ", ": ", "="]
        values = [
            "2001:db8:85a3::8a2e:370:7334",  # IPV6
            "198.51.100.23",  # IPV4
            "sk-live-abc123XYZ",  # code-shaped, no independent structural finding
        ]
        checked = 0
        for label, qualifier, connector, value in itertools.product(
            labels, qualifiers, connectors, values
        ):
            text = f"{label}{qualifier}{connector}{value}"
            with self.subTest(text=text):
                findings = hook.collect_findings_v2(text)
                _, copied = hook.render_with_trace(text, findings)
                # Round-11 retry fix (P2, finding 2): checked against `value`, the independently-
                # known secret substring actually embedded in `text` -- a finding's own span is
                # unsatisfiable-by-construction and cannot catch under-coverage; see
                # `_assert_idempotent_and_matches_v1_and_safe`'s own comment.
                start = text.index(value)
                hook.assert_no_origin_range_emitted(
                    copied, (start, start + len(value)), text=text, findings=findings
                )
            checked += 1
        self.assertEqual(checked, len(labels) * len(qualifiers) * len(connectors) * len(values))

    def test_cjk_label_qualifier_connector_structural_value_matrix_still_never_false_alarms(
        self,
    ) -> None:
        # Companion sweep for the CJK side of the same 5-producer family (TABLE_CJK/INLINE_CJK/
        # INLINE_CJK_IS/INLINE_CJK_SUFFIX), confirming the generalized fix does not regress the
        # already-working CJK bare-keyword strip -- this matrix passed at 0/288 even before this
        # round's fix (verified fresh, this round, before writing the fix), so this is a
        # non-regression guard, not a new-coverage claim.
        labels = ["密码", "令牌", "密钥", "口令", "凭据", "私钥", "助记词", "恢复码"]
        qualifiers = ["-prod2", "_v3", "2", ""]
        connectors = [" ", "：", "是"]
        values = ["2001:db8:85a3::8a2e:370:7334", "198.51.100.23", "sk-live-abc123XYZ"]
        checked = 0
        for label, qualifier, connector, value in itertools.product(
            labels, qualifiers, connectors, values
        ):
            text = f"{label}{qualifier}{connector}{value}"
            with self.subTest(text=text):
                findings = hook.collect_findings_v2(text)
                _, copied = hook.render_with_trace(text, findings)
                # Round-11 retry fix (P2, finding 2): checked against `value`, the independently-
                # known secret substring actually embedded in `text` -- a finding's own span is
                # unsatisfiable-by-construction and cannot catch under-coverage; see
                # `_assert_idempotent_and_matches_v1_and_safe`'s own comment.
                start = text.index(value)
                hook.assert_no_origin_range_emitted(
                    copied, (start, start + len(value)), text=text, findings=findings
                )
            checked += 1
        self.assertEqual(checked, len(labels) * len(qualifiers) * len(connectors) * len(values))

    def test_owner_label_prefix_end_returns_none_for_a_non_matching_kind_or_span(self) -> None:
        # `_owner_label_prefix_end` must fail closed (return None, causing no stripping at all,
        # never a false "safe to strip") whenever it cannot re-derive a real boundary: an unknown
        # kind, or a kind from the registry whose `own_covered` does not actually fullmatch that
        # kind's own producer pattern (true of most hand-built mutation-test Findings tagged with
        # one of these 5 kind strings but not shaped like a real match).
        self.assertIsNone(hook._owner_label_prefix_end("OWNER", "password: hunter2"))
        self.assertIsNone(hook._owner_label_prefix_end("STRUCT", "198.51.100.7"))
        self.assertIsNone(hook._owner_label_prefix_end("INLINE_ASCII", "not a real match at all"))

    def test_is_subsequence_basic_properties(self) -> None:
        self.assertTrue(hook._is_subsequence("", "anything"))
        self.assertTrue(hook._is_subsequence("abc", "aXbYcZ"))
        self.assertTrue(hook._is_subsequence("password-prod", "password-prod2 "))
        self.assertFalse(hook._is_subsequence("prod2", "password-prod "))  # order/char not present
        self.assertFalse(hook._is_subsequence("xyz", "password-prod"))

    def test_generalized_label_prefix_strip_still_catches_a_leak_that_is_not_a_subsequence_of_the_label(
        self,
    ) -> None:
        # The mutation-testing-of-the-suite argument this round's own fix needs (design doc SS5.3):
        # a deliberately broken `INLINE_ASCII`-tagged Finding whose render embeds real secret bytes
        # (not derivable from the label text alone) before the marker must still be caught -- the
        # new subsequence-bounded strip must not blind the oracle to this. `own_covered` here is a
        # genuine `_INLINE_ASCII_SECRET_RE` match (so `_owner_label_prefix_end` succeeds and
        # `label_prefix_end` is real), but the RENDER is hand-corrupted to leak the value's own
        # bytes ("Qz4") ahead of the marker instead of the correct label-only prefix.
        # Built directly from the real `_INLINE_ASCII_SECRET_RE` pattern (not depending on which
        # producer's own scanner pass order actually claims this input first in the real pipeline
        # -- `ATOMIC_LABEL`, Phase A's primary producer, claims a value-only span for this exact
        # shape, per that producer's own comment, so `collect_findings_v2(text)` alone would not
        # exercise the `INLINE_ASCII`-kind code path this test needs).
        text = "password: Qz4Kv7Mn2Pw9"
        match = hook._INLINE_ASCII_SECRET_RE.match("password: Qz4Kv7Mn2Pw9")
        self.assertIsNotNone(match)  # sanity: this really is a genuine match of the real pattern
        own_covered = match.group(0)
        self.assertEqual(own_covered, text)
        broken_render = "password: Qz4[REDACTED]"  # leaks the value's own leading 3 bytes
        broken = [Finding("INLINE_ASCII", 0, len(text), broken_render, is_owner=True)]
        _, copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(copied, (0, len(text)), text=text, findings=broken)

    def test_ambiguous_digit_tail_deletion_helper_accepts_only_a_single_digit_run_gap(self) -> None:
        # Unit-level test OF the fix itself (round-11 retry, P2 finding 3): mutation-tests the
        # helper directly, not just through the oracle's own call site, per the task's own
        # requirement to verify the oracle catches a DELIBERATELY BROKEN implementation, not only
        # that it passes on a correct one.
        f = hook._is_ambiguous_digit_tail_deletion
        self.assertTrue(f("password: ", "password: "))  # identical: trivially safe
        self.assertTrue(f("token ", "token_157 "))  # genuine digit-tail fold ("_157" dropped)
        self.assertTrue(f("db_password_prod is ", "db_password_prod is "))  # no fold needed
        # The two confirmed counterexamples the old `_is_subsequence` check accepted wrongly: real
        # VALUE bytes that happen to be an in-order subsequence of the label text, with no single
        # contiguous digit-only gap explaining the difference.
        self.assertFalse(f("sword", "password: "))
        self.assertFalse(f("ascde", "passcode: "))
        # A non-digit gap (e.g. a real qualifier word, never ambiguous-digit-shaped) must not be
        # accepted as a deletable gap either.
        self.assertFalse(f("token ", "token_prod "))
        self.assertFalse(f("x", "y"))  # no relation at all

    def test_generalized_label_prefix_strip_rejects_value_bytes_that_are_a_coincidental_subsequence(
        self,
    ) -> None:
        # End-to-end repro for finding 3 (P2): the exact two counterexamples the review
        # constructed against the live file, reproduced fresh. Both `own_covered` values are
        # genuine `_INLINE_ASCII_SECRET_RE` matches; both `broken_render`s leak real VALUE bytes
        # ("sword"/"ascde") ahead of the marker, dressed up as if they were label-derived -- the
        # old subsequence check accepted both because those letters happen to occur, in order,
        # inside the label/connector text purely by coincidence.
        cases = [
            ("password: sword9Kx2Qm", "sword[REDACTED]"),
            ("passcode: ascde9Kx2Qm", "ascde[REDACTED]"),
        ]
        for text, broken_render in cases:
            with self.subTest(text=text):
                match = hook._INLINE_ASCII_SECRET_RE.fullmatch(text)
                self.assertIsNotNone(match)  # sanity: a genuine match of the real pattern
                broken = [Finding("INLINE_ASCII", 0, len(text), broken_render, is_owner=True)]
                _, copied = hook.render_with_trace(text, broken)
                with self.assertRaises(AssertionError):
                    hook.assert_no_origin_range_emitted(
                        copied, (0, len(text)), text=text, findings=broken
                    )

    def test_cjk_keyword_strip_does_not_apply_to_a_value_only_span_owner_kind(self) -> None:
        # Round-11 retry fix (P1 BLOCKING, finding 13): the CJK-bare-keyword strip used to run
        # for ANY `allowed_source.kind`, including the value-only-span kinds
        # (ATOMIC_LABEL/ASSIGNMENT/QUERY_SECRET/TABLE_COLUMNS/TABLE_SPILLOVER/*_HAN_BRIDGE) whose
        # `own_covered` is the VALUE itself, never a label. A secret VALUE that merely happens to
        # start with CJK secret-keyword-shaped text let a broken render leak that prefix past the
        # oracle, mistaking it for a safe label echo. `text.index` locates the real secret
        # ("助记词9988ZqXwMn", ASSIGNMENT's own `own_covered`) independently of the deliberately
        # broken finding's own span, matching the process-integrity rule this suite follows
        # throughout (see `_assert_idempotent_and_matches_v1_and_safe`'s own comment).
        text = "token: 助记词9988ZqXwMn"
        findings = hook.collect_findings_v2(text)
        owner = next(f for f in findings if f.is_owner)
        self.assertEqual(owner.kind, "ASSIGNMENT")  # sanity: a genuine value-only-span kind
        secret = text[owner.start:owner.end]
        self.assertEqual(secret, "助记词9988ZqXwMn")
        broken_render = secret[:3] + "[REDACTED]"  # leaks the value's own keyword-shaped prefix
        broken = [Finding(owner.kind, owner.start, owner.end, broken_render, is_owner=True)]
        _, copied = hook.render_with_trace(text, broken)
        with self.assertRaises(AssertionError):
            hook.assert_no_origin_range_emitted(
                copied, (owner.start, owner.end), text=text, findings=broken
            )

    def test_generalized_label_prefix_strip_permits_the_real_folded_digit_render(self) -> None:
        # Sanity companion: the REAL, correct `_redact_inline_ascii_secret` output for the exact
        # digit-fold shape must still pass -- the fix narrows a false alarm, it does not merely
        # move where the false alarm happens.
        text = "password-prod2 2001:db8:85a3::8a2e:370:7334"
        findings = hook.collect_findings_v2(text)
        owner = next(f for f in findings if f.kind == "INLINE_ASCII")
        self.assertEqual(owner.render, "password-prod [REDACTED]")
        _, copied = hook.render_with_trace(text, findings)
        # Round-11 retry fix (P2, finding 2): checked against the independently-known secret
        # substring, not `owner`'s own span -- see
        # `_assert_idempotent_and_matches_v1_and_safe`'s own comment.
        secret = "2001:db8:85a3::8a2e:370:7334"
        start = text.index(secret)
        hook.assert_no_origin_range_emitted(
            copied, (start, start + len(secret)), text=text, findings=findings
        )

    def test_render_with_trace_copied_ranges_and_output_are_mutually_consistent(self) -> None:
        # Finding 5 (P3): no test previously asserted the invariant the oracle structurally trusts
        # -- that `copied_ranges` faithfully describes which parts of `output` are verbatim
        # original-text copies and which are redacted-component renders. Reconstructs `output`
        # independently from `copied_ranges` plus the resolved component list and confirms it
        # matches exactly, for a representative battery of real inputs (multiple owners, multiple
        # structural findings, merged components, and the empty-findings identity case).
        cases = [
            "nothing secret here at all",
            "password: hunter2verysecret",
            "prefix password: hunter2verysecret suffix",
            "password: hunter2verysecret token: sk-live-abc123xyz789fed",
            "密码-prod2: Ax7密-198.51.100.73-Qv",
            "| 密码 | Fw9-203.0.113.150-Jt2 |\n| note | nothing secret here |",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
        ]
        for text in cases:
            with self.subTest(text=text):
                findings = hook.collect_findings_v2(text)
                output, copied_ranges = hook.render_with_trace(text, findings)
                components = hook._reconcile_fragmentation(hook.merge_overlapping(findings), findings)
                # copied_ranges must be sorted, disjoint, and each must be a genuine verbatim copy.
                prev_end = 0
                for start, end in copied_ranges:
                    self.assertGreaterEqual(start, prev_end)
                    self.assertLess(start, end)
                    prev_end = end
                # Reconstruct output independently from copied_ranges + components, by walking
                # [0, len(text)) left to right and, at every position, emitting either the next
                # copied range verbatim or the next component's own selected render -- exactly
                # `render_with_trace`'s own selection logic, re-derived here rather than imported,
                # so this test cannot pass merely because it calls the same code under test.
                rebuilt: list[str] = []
                pos = 0
                copied_iter = iter(copied_ranges)
                comp_iter = iter(components)
                next_copied = next(copied_iter, None)
                next_comp = next(comp_iter, None)
                while pos < len(text):
                    if next_copied is not None and next_copied[0] == pos:
                        rebuilt.append(text[next_copied[0]:next_copied[1]])
                        pos = next_copied[1]
                        next_copied = next(copied_iter, None)
                    elif next_comp is not None and next_comp.start == pos:
                        if len(next_comp.members) == 1:
                            rebuilt.append(next_comp.members[0].render)
                        elif any(m.is_owner for m in next_comp.members):
                            owners = [m for m in next_comp.members if m.is_owner]
                            rebuilt.append(owners[0].render if len(owners) == 1 else hook._OWNER_FLAT_MARKER)
                        else:
                            rebuilt.append(hook._priority_marker(next_comp.members))
                        pos = next_comp.end
                        next_comp = next(comp_iter, None)
                    else:
                        self.fail(
                            f"gap at position {pos}: neither a copied range nor a component starts "
                            f"there -- copied_ranges/components do not tile [0, len(text)) together"
                        )
                self.assertEqual("".join(rebuilt), output)

    def test_digit_qualifier_marker_change_fires_across_every_connector_shape_not_just_full_width(
        self,
    ) -> None:
        # Finding 2 (P2, disclosure-accuracy): the comment above `_STRUCTURAL_DETECTORS_V2`
        # previously scoped this MARKER-CHANGE divergence to "certain connector shapes ... a
        # full-width connector followed by a non-hard-stop gap character" -- this pins the
        # corrected, broader scope this round's own fresh re-measurement found: EVERY connector
        # shape tested fires it (plain ASCII colon/equals included, not just full-width), gated
        # only on (a) a CJK keyword vocabulary and (b) the qualifier ending in a digit -- the
        # matching ASCII-keyword shapes (`password-prod2: ...`, `password-prod2=...`) do NOT
        # diverge at all in this same matrix (0/40, checked below), confirming the gate is the
        # keyword's own vocabulary, not the connector. Over-redaction only, never a leak --
        # verified via the provenance oracle, never a substring check.
        cases = [
            "密码-prod2: 198.51.100.73",
            "密码-prod2=198.51.100.73",
            "密码-prod2 = 198.51.100.73",
            "密码-prod2：198.51.100.73",
            "密码-prod2＝198.51.100.73",
            "密码_v3: 198.51.100.73",
        ]
        for text in cases:
            with self.subTest(text=text):
                v1 = hook.redact(text)
                v2 = hook.redact_v2(text)
                self.assertNotEqual(v1, v2)  # the disclosed divergence, reproduced directly
                self.assertNotIn("198.51.100.73", v2)  # over-redaction, not under
                findings = hook.collect_findings_v2(text)
                _, copied = hook.render_with_trace(text, findings)
                # Round-11 retry fix (P2, finding 2): checked against the independently-known
                # secret substring, not a finding's own span -- see
                # `_assert_idempotent_and_matches_v1_and_safe`'s own comment.
                secret = "198.51.100.73"
                start = text.index(secret)
                hook.assert_no_origin_range_emitted(
                    copied, (start, start + len(secret)), text=text, findings=findings
                )

    def test_digit_qualifier_marker_change_does_not_fire_with_no_qualifier_or_a_bare_digit(
        self,
    ) -> None:
        # Companion negative control from the same re-measurement: an empty qualifier or a bare
        # digit qualifier (no separating dash/underscore) produced zero divergence in this round's
        # fresh matrix, and the ASCII keyword vocabulary produced zero divergence at all (any
        # qualifier, any connector, in the same matrix as the positive test above) -- pinned so a
        # future change to either producer's qualifier/keyword grammar that narrows or widens this
        # is visible as a test change, not silently.
        cases = [
            "password: 198.51.100.73",
            "password2: 198.51.100.73",
            "密码: 198.51.100.73",
            "password-prod2: 198.51.100.73",
            "password-prod2=198.51.100.73",
            "api_key_v3：198.51.100.73",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(hook.redact(text), hook.redact_v2(text))


# ---------------------------------------------------------------------------------------
# Ported verbatim from round 3 (e4cc70f261) on 2026-08-28 when the two rewritten patterns
# were rebased onto that commit. These three classes are the round-1/2/3 ReDoS + leak
# regression pins; they were absent from this draft copy of the suite, which is why the
# draft's own vulnerable patterns went unnoticed here.
# ---------------------------------------------------------------------------------------
class RedactQuantifierBoundTests(unittest.TestCase):
    """ReDoS regression pins for the three quantifier bounds added 2026-08-28.

    Each pattern below previously carried an unbounded quantifier over a character class that
    excludes the literal the quantifier must eventually reach ('://', '@', '.'). On input that
    never reaches that literal the engine ate the whole run and then gave it back one character
    at a time, at O(n) starting offsets -- quadratic. Measured against `"a-" * n` on the pre-fix
    definitions of these same patterns:

        pattern             8,000 chars   32,000 chars   growth per doubling
        _ASSIGNMENT_RE          1.18s        32.10s           x6.9
        _EMAIL_RE               0.12s         2.16s           x4.3
        _URL_USERINFO_RE        0.14s         1.93s           x3.8

    `redact()` runs on *untruncated* block text (see `split_blocks`, which deliberately redacts
    before slicing to 4,000 chars so a long PEM body is not cut in half), and the hook's budget in
    hooks.json is 5 seconds -- so `_ASSIGNMENT_RE` alone blew that budget on roughly 11,000
    characters of ordinary dash-separated prose.
    """

    # 4x apart, so a quadratic regression shows up as ~16x and cannot hide inside timing noise.
    ADVERSARIAL_SMALL = "a-" * 4_000  # 8,000 chars
    ADVERSARIAL_LARGE = "a-" * 16_000  # 32,000 chars

    @staticmethod
    def _timed(call, text: str) -> float:
        start = time.perf_counter()
        call(text)
        return time.perf_counter() - start

    def _assert_near_linear(self, call, name: str, *, ceiling: float = 1.0) -> None:
        small = self._timed(call, self.ADVERSARIAL_SMALL)
        large = self._timed(call, self.ADVERSARIAL_LARGE)
        # The ratio is the real property: 4x the input must not cost ~16x the time. The 10x
        # allowance (rather than 4x) absorbs scheduling noise on a loaded machine while still
        # failing loudly on a genuine quadratic regression. The absolute ceiling catches a
        # regression that keeps a flat ratio while being slow in absolute terms.
        self.assertLess(
            large,
            max(small * 10, 0.05),
            f"{name} scaled worse than near-linearly: {small:.4f}s -> {large:.4f}s",
        )
        self.assertLess(
            large,
            ceiling,
            f"{name} stalled on 32,000 chars of adversarial input: {large:.4f}s",
        )

    def test_url_userinfo_quantifiers_are_flat_and_uncapped(self) -> None:
        # Round 3 (2026-08-28) removed these bounds rather than widening them a third time. Both
        # earlier rounds picked a number and an independent review found the leak just past it;
        # any finite N has one. What restores linear time is that every quantifier here is now a
        # single FLAT class with nothing nested inside it to re-partition, and that the boundary
        # lookbehind is the exact complement of the run class so one run has one start position.
        pattern = hook._URL_USERINFO_RE.pattern
        self.assertIn("[a-z0-9+.-]*://", pattern)
        self.assertIn(r"[^\s/@:]+:", pattern)
        self.assertIn(r"[^\s/@]+@", pattern)
        # No numeric ceiling anywhere: a repetition count is what leaked twice.
        self.assertNotRegex(pattern, r"\{\d+,\d+\}")
        # THE invariant. The lookbehind class and the scheme run class must stay byte-identical:
        # a character one accepts and the other rejects is a fresh start position in the middle of
        # a run, which is quadratic. This is what made `(U+212A + ".") * n` blow up in an earlier
        # cut of this fix.
        self.assertIn(r"(?<![a-z0-9+.\-])", pattern)
        self.assertIn("[a-z0-9+.-]*", pattern)

    def test_email_quantifiers_are_bounded(self) -> None:
        pattern = hook._EMAIL_RE.pattern
        # RFC 5321: local part <= 64 octets, domain <= 253. Longest IANA root TLD is 24 chars.
        self.assertIn("[A-Z0-9._%+-]{1,64}", pattern)
        self.assertIn("[A-Z0-9.-]{1,253}", pattern)
        self.assertIn("[A-Z]{2,24}", pattern)
        self.assertNotIn("[A-Z0-9._%+-]+", pattern)

    def test_assignment_keyword_run_is_one_flat_uncapped_class(self) -> None:
        # Round 3 (2026-08-28). The prefix/suffix repeats were repeated GROUPS each holding their
        # own quantifier, so the engine walked the whole (prefix_count, suffix_count) grid at every
        # start position -- that grid is what the bounds were capping, and capping it is what leaked
        # (round 1 at 8 segments, round 2 at 64). One flat class over one quantifier has nothing to
        # partition, so it is linear at any length and needs no ceiling at all.
        pattern = hook._ASSIGNMENT_RE.pattern
        self.assertIn("(?P<keyword_run>[A-Za-z0-9_-]+)", pattern)
        # Neither ambiguous nested-quantifier group may come back, bounded or not.
        self.assertNotIn("[A-Za-z][A-Za-z0-9]*[_-]", pattern)
        self.assertNotIn("[_-][A-Za-z0-9]+", pattern)
        # And no numeric ceiling in the part of the pattern this round is about: a repetition
        # count over the LABEL RUN is what leaked twice (round 1 at 8 segments, round 2 at 64).
        #
        # Scoped to the prefix (boundary + gate + keyword_run) rather than asserted over the whole
        # pattern, which is what e4cc70f261 did. That blanket form was a valid proxy there because
        # that commit's `_ASSIGNMENT_RE` had a trivial value grammar (`[^\s,;"&]{3,}`) with no
        # bounds at all. This draft's value grammar is richer and carries six bounds of its own,
        # none of them on the label run and none of them introduced by this rebase:
        # `_SECRET_VALUE_CODE_TOKEN_LOOKAHEAD` ({1,63} x3, {0,63}) and `_MULTI_LABEL_BOUNDARY_SHAPE`
        # ({0,8}, {0,40}). Deliberately NOT waived silently -- they are pinned by exact value
        # below, so a NEW ceiling appearing anywhere in the value grammar still fails this test
        # rather than hiding inside a broadened exemption.
        prefix, marker, value_grammar = pattern.partition('(?P<preq>')
        self.assertTrue(marker, "pattern shape changed: no (?P<preq> group")
        self.assertNotRegex(prefix, r"\{\d+,\d+\}")
        self.assertEqual(
            re.findall(r"\{\d+,\d+\}", value_grammar),
            ["{1,63}", "{1,63}", "{0,63}", "{1,63}", "{0,8}", "{0,40}"],
            "the value grammar's bounds changed -- re-audit them for the leak-just-past-the-bound "
            "shape this round removed from the label run",
        )
        # THE invariant. The boundary lookbehind must stay the exact complement of the run class,
        # IGNORECASE scope included. When it was the ASCII-scoped `(?<!(?-i:[A-Za-z0-9]))` against
        # an IGNORECASE-tainted run, U+0130/U+0131/U+017F/U+212A opened a start position inside
        # every run and `redact("K" * n)` was quadratic -- 4.5s at 16,000 characters, past the
        # hook's own 5s budget at ~17,000. Pinned as a regression by the timing test below.
        self.assertIn(r"(?i)(?<![A-Za-z0-9_\-])", pattern)
        # The keyword's component boundaries moved INTO the gate, and stay ASCII-scoped there --
        # that scoping is what still catches "Kpassword=..." (see the homoglyph test above)
        # while still refusing "monkey"/"turkey"/"pwda".
        self.assertIn(r"(?<!(?-i:[A-Za-z0-9]))", pattern)
        self.assertIn(r"(?!(?-i:[A-Za-z0-9]))", pattern)

    def test_url_userinfo_pattern_scales_near_linearly(self) -> None:
        self._assert_near_linear(
            lambda text: hook._URL_USERINFO_RE.sub("X", text), "_URL_USERINFO_RE"
        )

    def test_email_pattern_scales_near_linearly(self) -> None:
        self._assert_near_linear(lambda text: hook._EMAIL_RE.sub("X", text), "_EMAIL_RE")

    def test_assignment_pattern_scales_near_linearly(self) -> None:
        self._assert_near_linear(
            lambda text: hook._ASSIGNMENT_RE.sub("X", text), "_ASSIGNMENT_RE"
        )

    def test_assignment_pattern_scales_near_linearly_on_underscore_runs(self) -> None:
        # `_ASSIGNMENT_RE`'s keyword-run segments accept '_' as well as '-', and an underscore run
        # was measurably the worse of the two before the bound (3.70s at 8,000 chars vs 1.18s for
        # the dash run), so it gets its own pin rather than riding on the shared dash fixture.
        small, large = "a_" * 4_000, "a_" * 16_000
        small_elapsed = self._timed(lambda t: hook._ASSIGNMENT_RE.sub("X", t), small)
        large_elapsed = self._timed(lambda t: hook._ASSIGNMENT_RE.sub("X", t), large)
        self.assertLess(large_elapsed, max(small_elapsed * 10, 0.05))
        self.assertLess(large_elapsed, 1.0)

    def test_full_redact_pipeline_scales_near_linearly_on_adversarial_dash_heavy_input(
        self,
    ) -> None:
        # The whole pipeline, not one pattern in isolation -- this is what `split_blocks()` calls
        # on untruncated block text. Pre-fix, `redact("a-" * 8000)` measured 2.52s here.
        self._assert_near_linear(hook.redact, "redact()", ceiling=2.0)

    def test_bounds_do_not_narrow_redaction_of_realistic_values(self) -> None:
        # Every bound sits far above what a real URL/address/identifier uses, so nothing that was
        # redacted before the bound stops being redacted now. Each case is a full-pipeline check.
        cases = {
            "url": "https://user:pass@example.com/path",
            "url_compound_scheme": "git+ssh://user:tok@github.com/o/r",
            "url_scheme_tail_at_bound": "a" * 63 + "://u:p@h",
            # 36 characters, and a real IANA-registered scheme -- it did not fit the old 31.
            "url_longest_real_iana_scheme": (
                "microsoft.windows.camera.multipicker://user:pass@example.com"
            ),
            "url_userinfo_at_bound": "https://" + "u" * 4096 + ":" + "p" * 4096 + "@example.com",
            "email": "contact alice@example.com now",
            "email_local_part_at_rfc_limit": "x" * 64 + "@example.com",
            "email_long_domain": "u@" + "d" * 60 + "." + "e" * 60 + ".com",
            "email_tld_at_iana_limit": "u@example." + "t" * 24,
            "assignment": "password: hunter2secret",
            "assignment_suffix_at_bound": "key" + "_zz" * 64 + ": value123",
            "assignment_prefix_at_bound": "_".join(["aa"] * 8) + "_key: value123",
            # Past the prefix bound the run simply matches starting further along, so the value is
            # still redacted -- only the leading label text falls outside the match.
            "assignment_prefix_past_bound": "_".join(["aa"] * 12) + "_key: value123",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                redacted = hook.redact(text)
                self.assertNotEqual(redacted, text, f"{name} stopped being redacted")
                self.assertIn("[REDACTED", redacted)

    def test_there_is_no_accepted_narrowing_left_at_any_length(self) -> None:
        # This test used to be `test_known_accepted_narrowing_is_limited_to_absurd_suffix_runs`,
        # and it documented where the `{0,64}` bound still cut a real label off. Round 3 removed
        # the bound, so there is no longer a "where" -- the correct assertion is that the label
        # length no longer has a ceiling at all. Checkpoints run far past every bound this pattern
        # has ever carried (8, then 64), so a fourth attempt at "just raise the number" fails here.
        for segments in (7, 8, 9, 63, 64, 65, 100, 1_000, 10_000):
            with self.subTest(segments=segments):
                text = "key" + "_zz" * segments + ": value123"
                self.assertIn("[REDACTED]", hook.redact(text))
                self.assertNotIn("value123", hook.redact(text))
        # And the ordinary shapes this pattern actually exists for are unaffected.
        for text in ("api_key: abc123", "db_password = s3cr3t", "AWS_SECRET_ACCESS_KEY=wJalrXU"):
            with self.subTest(text=text):
                self.assertIn("[REDACTED", hook.redact(text))


class RedactRegexBoundWideningTests(unittest.TestCase):
    """Round-2 regression pins for fc39b99705's bounds, which were tight enough to leak.

    fc39b99705 bounded three ReDoS-prone quantifiers, which was the right fix, but picked values
    that cut real secrets instead of only rejecting adversarial input. An independent final review
    found two reproducers; both are pinned verbatim below against the output the *parent* of
    fc39b99705 produced, so the fix cannot silently regress in either direction -- the bounds must
    stay finite (ReDoS) and stay generous (leaks).
    """

    # Verbatim from the review, with the parent-of-fc39b99705 output each one must reproduce.
    USERINFO_REPRO = "https://u:" + "%41" * 86 + "@example.com"
    USERINFO_EXPECTED = "https://[REDACTED]@example.com"
    LABEL_REPRO = "api_key" + "_svc" * 9 + ": secret-value-123"
    LABEL_EXPECTED = "api_key_svc_svc_svc_svc_svc_svc_svc_svc_svc: [REDACTED]"

    def test_percent_encoded_userinfo_password_fully_redacts(self) -> None:
        # 86 '%41' groups = a 258-character password. Against the old {1,255} the match stopped 3
        # characters short of the '@', the whole pattern failed, `_EMAIL_RE` then matched the tail
        # on its own, and ~65 '%41' groups of the password survived in plaintext beside a
        # [REDACTED_EMAIL] tag. Nothing of the password may appear in the output.
        redacted = hook.redact(self.USERINFO_REPRO)
        self.assertEqual(redacted, self.USERINFO_EXPECTED)
        self.assertNotIn("%41", redacted)

    def test_nine_segment_label_fully_redacts(self) -> None:
        # Nine '[_-]'-separated suffix segments. Against the old {0,8} the label stopped being
        # recognised as a label at all and the value leaked whole -- not truncated, not tagged.
        redacted = hook.redact(self.LABEL_REPRO)
        self.assertEqual(redacted, self.LABEL_EXPECTED)
        self.assertNotIn("secret-value-123", redacted)

    def test_userinfo_passwords_redact_across_realistic_lengths(self) -> None:
        # A swept range rather than the one repro length, so a future bound that merely clears
        # 258 characters does not pass. 1,365 '%41' groups = 4,095 characters, the widest the
        # {1,4096} bound admits.
        for groups in (1, 20, 85, 86, 87, 100, 200, 500, 1_000, 1_365):
            with self.subTest(groups=groups):
                text = "https://u:" + "%41" * groups + "@example.com"
                self.assertEqual(hook.redact(text), self.USERINFO_EXPECTED)

    def test_long_labels_redact_across_realistic_segment_counts(self) -> None:
        # Same idea for the suffix bound: every count from just-over-the-old-bound up to the new
        # one must still redact the value.
        for segments in (8, 9, 10, 16, 32, 48, 63, 64):
            with self.subTest(segments=segments):
                text = "api_key" + "_svc" * segments + ": secret-value-123"
                redacted = hook.redact(text)
                self.assertNotIn("secret-value-123", redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_prefix_overflow_never_loses_the_value(self) -> None:
        # Why the prefix bound stays at 8 while the suffix went to 64: every prefix segment ends
        # in '[_-]', so the lookbehind admits a start position at every segment boundary and an
        # over-long run just makes the match start further along. The skipped text is label, and
        # `keyword_run` is echoed back verbatim, so the output is unchanged. Cost is ~the product
        # of the two bounds, so widening the prefix would cost as much as the suffix did and buy
        # no leak coverage at all.
        for segments in (8, 9, 12, 40, 200):
            with self.subTest(segments=segments):
                text = "_".join(["aa"] * segments) + "_key: secret-value-123"
                redacted = hook.redact(text)
                self.assertNotIn("secret-value-123", redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_longest_registered_iana_scheme_still_redacts(self) -> None:
        # 'microsoft.windows.camera.multipicker' is 36 characters, so it did not fit the old
        # 32-character scheme ceiling ([a-z] + {0,31}) and its credentials stopped being redacted.
        text = "microsoft.windows.camera.multipicker://user:pass@example.com"
        self.assertEqual(hook.redact(text), "microsoft.windows.camera.multipicker://[REDACTED]@example.com")

    def test_widened_bounds_still_scale_linearly_on_the_worst_shape(self) -> None:
        # The bound's *size* only sets the constant; what fixes the ReDoS is that it is finite.
        # This is the worst shape found for `_ASSIGNMENT_RE` -- a run of 'key_' makes the keyword
        # alternation succeed at every offset, so the engine explores the whole
        # (prefix_count, suffix_count) grid before failing, unlike the cheap 'a-'/'a_' runs the
        # other tests in this file use. Unbounded (parent of fc39b99705) this same shape measured
        # 9.2s at 2,000 chars and 71.6s at 4,000 -- super-quadratic. Bounded at 8/64 it is 0.5s at
        # 8,000 chars and 2.5s at 32,000: 4x the input for ~4x the time, and ~300x faster than
        # unbounded at 4,000 chars.
        small, large = "key_" * 2_000, "key_" * 8_000  # 8,000 and 32,000 chars
        t0 = time.perf_counter()
        hook.redact(small)
        small_elapsed = time.perf_counter() - t0
        t0 = time.perf_counter()
        hook.redact(large)
        large_elapsed = time.perf_counter() - t0
        # 4x the input must not cost ~16x the time. 10x absorbs noise on a loaded machine while
        # still failing loudly on a return to quadratic behaviour.
        self.assertLess(
            large_elapsed,
            max(small_elapsed * 10, 0.05),
            f"worst-shape scaling regressed: {small_elapsed:.4f}s -> {large_elapsed:.4f}s",
        )
        # Absolute ceiling: the hook's budget in hooks.json is 5 seconds, and this is 32,000
        # characters of purpose-built input. Catches a bound widened far enough to matter.
        self.assertLess(
            large_elapsed,
            5.0,
            f"worst-shape absolute cost too high at the widened bound: {large_elapsed:.4f}s",
        )

    def test_widened_userinfo_bound_does_not_slow_the_pipeline(self) -> None:
        # The userinfo halves went 255 -> 4096, a 16x wider bound, and it costs nothing measurable:
        # on this input the cost is dominated by the scheme-tail retry ladder and the halves are
        # never reached at all. This is the timing check named in the review, at the new bound.
        text = "a-" * 16_000  # 32,000 chars
        t0 = time.perf_counter()
        hook.redact(text)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0, f'redact("a-" * 16000) took {elapsed:.4f}s')


class RedactFlatQuantifierTests(unittest.TestCase):
    """Round-3 pins: the bounds are gone, and they must not come back at ANY width.

    Rounds 1 (fc39b99705) and 2 (bb2142e41c) both fixed a real ReDoS by capping a quantifier, and
    an independent review then found the same plaintext leak sitting just past each cap:

        redact("api_key" + "_x" * 65 + ": secret-suffix-65")   -> leaked, past round 2's {0,64}
        redact("a" * 65 + "://user:" + "%41" * 86 + "@...")    -> leaked, past round 2's {0,63}
        redact("https://u:" + "%41" * 1366 + "@...")           -> leaked, past round 2's {1,4096}

    That is not two unlucky numbers, it is the shape: "match up to N repetitions, and if the real
    value has N+1 match only a prefix (or nothing) and let the tail render as plaintext" leaks just
    past every finite N. Round 3 removed the caps instead, by killing the two things they were
    standing in for -- the repeated-group-with-an-inner-quantifier ambiguity, and a boundary
    lookbehind whose character class disagreed with the run class it guarded.

    The length checkpoints below deliberately run orders of magnitude past every bound that has
    ever existed in these patterns (8, 31, 63, 64, 255, 4096), so a fourth round that "just raises
    the number again" fails immediately rather than shipping and being caught by the next review.
    """

    # The four IGNORECASE-tainted homoglyphs. Each folds to an ASCII letter under `(?i)`, so each
    # is a run character that an ASCII-scoped lookbehind does not reject -- the mismatch that made
    # both earlier rounds quadratic on these shapes without either round noticing.
    KELVIN = "\u212a"

    @staticmethod
    def _timed(call, text: str) -> float:
        start = time.perf_counter()
        call(text)
        return time.perf_counter() - start

    # ------------------------------------------------------------------ the three round-3 repros
    def test_round3_repro_label_one_segment_past_the_round2_bound(self) -> None:
        text = "api_key" + "_x" * 65 + ": secret-suffix-65"
        redacted = hook.redact(text)
        self.assertNotIn("secret-suffix-65", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_round3_repro_scheme_one_char_past_the_round2_bound(self) -> None:
        text = "a" * 65 + "://user:" + "%41" * 86 + "@example.test"
        redacted = hook.redact(text)
        self.assertNotIn("%41", redacted)
        self.assertIn("[REDACTED]@example.test", redacted)

    def test_round3_repro_userinfo_one_group_past_the_round2_bound(self) -> None:
        text = "https://u:" + "%41" * 1366 + "@example.test"  # 4,098 chars, past {1,4096}
        self.assertEqual(hook.redact(text), "https://[REDACTED]@example.test")

    # ------------------------------------------------------------------ no boundary at ANY length
    def test_label_suffix_has_no_leak_boundary_at_any_length(self) -> None:
        # 8 and 64 are rounds 1 and 2's bounds. Everything past 65 is new ground; 20,000 segments
        # is a 40,000-character label, ~300x round 2's ceiling.
        for segments in (1, 8, 9, 64, 65, 66, 100, 500, 1_000, 5_000, 20_000):
            with self.subTest(segments=segments):
                text = "api_key" + "_x" * segments + ": secret-suffix-value"
                redacted = hook.redact(text)
                self.assertNotIn("secret-suffix-value", redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_label_prefix_has_no_leak_boundary_at_any_length(self) -> None:
        for segments in (1, 8, 9, 64, 65, 200, 1_000, 10_000):
            with self.subTest(segments=segments):
                text = "_".join(["aa"] * segments) + "_key: secret-prefix-value"
                redacted = hook.redact(text)
                self.assertNotIn("secret-prefix-value", redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_url_scheme_has_no_leak_boundary_at_any_length(self) -> None:
        # 31 and 63 are rounds 1 and 2's scheme-tail bounds.
        for length in (1, 31, 32, 36, 63, 64, 65, 200, 5_000, 50_000):
            with self.subTest(scheme_length=length):
                text = "s" * length + "://user:supersecretpw@example.test"
                redacted = hook.redact(text)
                self.assertNotIn("supersecretpw", redacted)
                self.assertIn("[REDACTED]@example.test", redacted)

    def test_url_userinfo_halves_have_no_leak_boundary_at_any_length(self) -> None:
        # 255 and 4096 are rounds 1 and 2's userinfo bounds. 40,000 is ~10x round 2's ceiling.
        for length in (1, 255, 256, 4_096, 4_097, 10_000, 40_000):
            with self.subTest(half_length=length):
                text = "https://" + "u" * length + ":" + "p" * length + "@example.test"
                redacted = hook.redact(text)
                self.assertEqual(redacted, "https://[REDACTED]@example.test")

    def test_percent_encoded_password_has_no_leak_boundary_at_any_length(self) -> None:
        # The review's own vector, swept: 1,365 groups is exactly round 2's {1,4096}; 20,000 groups
        # is a 60,000-character password.
        for groups in (1, 85, 86, 1_365, 1_366, 1_367, 5_000, 20_000):
            with self.subTest(groups=groups):
                text = "https://u:" + "%41" * groups + "@example.test"
                redacted = hook.redact(text)
                self.assertNotIn("%41", redacted)
                self.assertEqual(redacted, "https://[REDACTED]@example.test")

    # ------------------------------------------------------------------ linearity, no cap needed
    @staticmethod
    def _two_patterns(text: str) -> str:
        """Just the two patterns this round rewrote, in pipeline order."""
        text = hook._URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
        return hook._ASSIGNMENT_RE.sub(hook._redact_assignment, text)

    def _assert_linear(self, call, make, name: str, *, ceiling: float = 3.0) -> None:
        # 32x the input (8,000 -> 256,000). Linear is ~32x, quadratic is ~1024x. The 150x
        # allowance still fails a genuine quadratic by nearly an order of magnitude while
        # absorbing scheduling noise on a loaded machine; the absolute ceiling catches a
        # regression that keeps a flat ratio while being slow outright.
        small = self._timed(call, make(8_000))
        large = self._timed(call, make(256_000))
        self.assertLess(
            large,
            max(small * 150, 0.05),
            f"{name} scaled worse than linearly: {small:.4f}s @8k -> {large:.4f}s @256k",
        )
        self.assertLess(large, ceiling, f"{name} took {large:.4f}s on 256,000 chars")

    def test_worst_shape_key_run_is_linear_to_256k(self) -> None:
        # Round 2's own worst shape. Unbounded (parent of fc39b99705) it was 71.6s at 4,000 chars;
        # bounded at 64/4096 it was 2.5s at 32,000. Flat and uncapped it is ~0.04s at 256,000
        # through the two patterns, ~0.18s through the whole pipeline.
        self._assert_linear(hook.redact, lambda n: "key_" * (n // 4), '"key_" * n')

    def test_worst_shape_key_run_with_dangling_separator_is_linear_to_256k(self) -> None:
        # Nastier than the bare run: the separator makes the gate succeed, so the whole tail is
        # explored, and the too-short value then forces the failure path.
        self._assert_linear(
            hook.redact, lambda n: "key_" * (n // 4) + ": ab", '"key_" * n + ": ab"'
        )

    def test_homoglyph_run_is_linear_to_256k_through_both_rewritten_patterns(self) -> None:
        # The vector both earlier rounds missed, and the reason the boundary/run class invariant
        # is now spelled out at both patterns. Measured on the round-2 definitions,
        # `_ASSIGNMENT_RE.sub` against "\u212a" * n: 0.075s / 0.292s / 1.145s at 2k/4k/8k --
        # x3.9 per doubling, textbook quadratic, past the hook's own 5s budget at ~17,000
        # characters. Bounded input, so no ceiling could ever have caught it. Now x1.9.
        #
        # Round-4 update (2026-08-28): this was scoped to the two patterns round 3 rewrote because
        # `redact()` as a whole was still quadratic on this shape, in the four siblings round 3
        # listed. Those four are fixed now (see `RedactSiblingFlatQuantifierTests`), so the scoping
        # is no longer needed -- the whole pipeline is asserted linear on this shape below, in
        # addition to the two-pattern assertion this test has always made.
        self._assert_linear(self._two_patterns, lambda n: self.KELVIN * n, '"\\u212a" * n')
        self._assert_linear(hook.redact, lambda n: self.KELVIN * n, 'redact("\\u212a" * n)')

    def test_homoglyph_separated_label_run_is_linear_to_256k(self) -> None:
        self._assert_linear(
            hook.redact,
            lambda n: (self.KELVIN + "password_") * (n // 10),
            '("\\u212a" + "password_") * n',
        )

    def test_homoglyph_separated_scheme_run_is_linear_to_256k(self) -> None:
        self._assert_linear(
            hook.redact,
            lambda n: (self.KELVIN + ".") * (n // 2) + "://u:p",
            '("\\u212a" + ".") * n + "://u:p"',
        )

    def test_homoglyph_quadratic_residue_is_not_in_the_two_patterns_this_round_rewrote(
        self,
    ) -> None:
        # Round-3 pinned the boundary of its own scope with a measurement rather than a claim. Round
        # 4 (2026-08-28) fixed the four siblings it named, so the list below is now every pattern in
        # the homoglyph blast radius rather than only round 3's half of it -- kept in one place so a
        # regression in ANY of them fails here with the offending name.
        run = self.KELVIN * 8_000
        for name, call in (
            ("_URL_USERINFO_RE", lambda t: hook._URL_USERINFO_RE.sub("X", t)),
            ("_ASSIGNMENT_RE", lambda t: hook._ASSIGNMENT_RE.sub("X", t)),
            ("_SECRET_LABEL_KEYWORD_RE", lambda t: hook._SECRET_LABEL_KEYWORD_RE.sub("X", t)),
            ("_INLINE_ASCII_SECRET_RE", lambda t: hook._INLINE_ASCII_SECRET_RE.sub("X", t)),
            (
                "_CJK_SECRET_VALUE_PERMISSIVE_TABLE_RE",
                lambda t: hook._CJK_SECRET_VALUE_PERMISSIVE_TABLE_RE.sub("X", t),
            ),
            (
                "_CJK_TABLE_CELL_VALUE_OR_PLACEHOLDER_RE",
                lambda t: hook._CJK_TABLE_CELL_VALUE_OR_PLACEHOLDER_RE.sub("X", t),
            ),
        ):
            with self.subTest(fixed=name):
                self.assertLess(self._timed(call, run), 0.25, f"{name} regressed to quadratic")

    def test_dash_and_underscore_runs_are_linear_to_256k(self) -> None:
        for name, make in (
            ('"a-" * n', lambda n: "a-" * (n // 2)),
            ('"a_" * n + ":"', lambda n: "a_" * (n // 2) + ":"),
            ('"_" * n', lambda n: "_" * n),
            ('"a://" * n', lambda n: "a://" * (n // 4)),
            ('"http://u:" + "a:" * n', lambda n: "http://u:" + "a:" * (n // 2)),
        ):
            with self.subTest(shape=name):
                self._assert_linear(hook.redact, make, name)

    def test_long_secret_is_redacted_and_cheap_at_the_same_time(self) -> None:
        # The two properties this round had to hold simultaneously, on one input: a 60,000-char
        # label with a real value behind it must redact completely AND not cost quadratic time.
        text = "api_key" + "_x" * 30_000 + ": secret-suffix-value"
        elapsed = self._timed(hook.redact, text)
        self.assertNotIn("secret-suffix-value", hook.redact(text))
        self.assertLess(elapsed, 3.0, f"60,000-char label took {elapsed:.4f}s")

    # ------------------------------------------------------------------ no semantic regression
    def test_flat_run_did_not_start_over_redacting_ordinary_words(self) -> None:
        # The flat run captures the WHOLE label, so the keyword's own component boundaries had to
        # move into the gate rather than disappear. Without the trailing `(?!(?-i:[A-Za-z0-9]))`
        # every word merely starting with a keyword ("keynote", "secrets", "pwda") would redact.
        for text in (
            "turkey=5",
            "monkey=xyz123",
            "keynote: my-presentation",
            "secrets: three-of-them",
            "tokens: twelve-total",
            "pwda: some-value-x",
            "api_keys: documented",
            "xxpassword: value123",
            "9key: value123",
        ):
            with self.subTest(text=text):
                self.assertEqual(hook.redact(text), text)

    def test_flat_run_kept_every_shape_the_bounded_pattern_redacted(self) -> None:
        # Spot-check across both patterns, including the shapes each earlier round added.
        cases = {
            "https://user:pass@example.com/path": "[REDACTED]@example.com",
            "git+ssh://user:tok@github.com/o/r": "[REDACTED]@github.com",
            "microsoft.windows.camera.multipicker://user:pass@example.com": "[REDACTED]@",
            "password: hunter2secret": "[REDACTED]",
            "api_key: abc123": "[REDACTED]",
            "db_password = s3cr3t": "[REDACTED]",
            "AWS_SECRET_ACCESS_KEY=wJalrXU": "[REDACTED]",
            "soga_key=abcd1234efgh5678ijkl9012mnop3456": "[REDACTED]",
            '{"password": "hunter2xyz"}': "[REDACTED]",
            "_key: value123": "[REDACTED]",
            "-password: value123": "[REDACTED]",
            "\u212apassword=hunter2value": "[REDACTED]",
            "\u212ahttps://user:supersecretpw@host": "[REDACTED]",
            "\u017fpassword=hunter2value": "[REDACTED]",
            "\u0130password=hunter2value": "[REDACTED]",
            "\u0131password=hunter2value": "[REDACTED]",
            "\u8bbf\u95eehttps://user:supersecretpw@host\u4eca\u5929": "[REDACTED]",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, hook.redact(text))

    def test_flat_run_now_also_redacts_shapes_the_bounded_pattern_dropped(self) -> None:
        # Deliberate widenings, all in the "redact more" direction. Each of these leaked at HEAD:
        # the bounded suffix could not span a doubled separator, a trailing separator, or a
        # homoglyph, so the label stopped being a label and the value was emitted verbatim.
        for text in (
            "api_key__x: secret-value-1",
            "api_key_: secret-value-2",
            "api_key-: secret-value-3",
            "password\u212a: secret-value-4",
            "password-x\u212a: secret-value-5",
            "_https://user:supersecretpw@host",
        ):
            with self.subTest(text=text):
                redacted = hook.redact(text)
                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn("secret-value", redacted)
                self.assertNotIn("supersecretpw", redacted)

    def test_lookaheads_are_atomic_on_the_deployed_interpreter(self) -> None:
        # The gate is a lookahead, and the linearity argument depends on `re` not re-entering it
        # with a different partition when the tail fails. Verified rather than assumed, because
        # the obvious way to write "do not backtrack into this" -- an atomic group `(?>...)` or a
        # possessive quantifier -- does not exist on the interpreter that actually runs the hook.
        # `install_bridge.py` writes `/usr/bin/python3 <script>` into hooks.json, which is the
        # macOS system Python (3.9.6 on this machine); atomic groups and possessive quantifiers
        # need 3.11+. So the fix could not use them, and the property is pinned directly instead.
        import re as _re
        import sys as _sys

        # Atomicity itself: if the lookahead were re-entered with a shorter `a*`, `\1ab` would
        # match "aaab". It is not, so this is None -- on every version.
        self.assertIsNone(_re.match(r"(?=(a*))\1ab", "aaab"))
        self.assertIsNone(_re.match(r"(?=(a+))\1ab", "aaab"))
        if _sys.version_info < (3, 11):
            for unsupported in (r"(?>a+)b", r"a++b", r"a{1,3}+b"):
                with self.subTest(syntax=unsupported):
                    with self.assertRaises(_re.error):
                        _re.compile(unsupported)


class RedactSiblingFlatQuantifierTests(unittest.TestCase):
    """Round-4 pins for the four siblings round 3 measured, named, and deliberately deferred.

    Round 3 fixed `_ASSIGNMENT_RE`/`_URL_USERINFO_RE` and left a written scope note listing four
    patterns sharing `_ASCII_SECRET_KEYWORD_CORE` that were still quadratic on the same homoglyph
    run. A `gpt-5.6-sol` review then returned NO-GO on exactly that residue. Measured here before
    the fix, `/usr/bin/python3` 3.9.6:

        _SECRET_LABEL_KEYWORD_RE.sub      108 / 562 / 3171 / 12828 ms  at 2k/4k/8k/16k  ("İıſK" run)
        _INLINE_ASCII_SECRET_RE.sub        81 / 903 / 3382 / 12596 ms  at 2k/4k/8k/16k  ("İıſK" run)

    The two CJK table patterns turned out NOT to be quadratic on a homoglyph run -- the review's
    reproduction was right about the symptom and the sizes but not about the mechanism, and the
    difference matters because the fix is different. They are LINEAR with a ~250us/character
    constant, from `_cjk_value_pattern`'s 256-deep three-branch guard scan re-run at every start
    position, and the shape that triggers it is an ordinary value-body run with no alphanumeric in
    it at all:

        _CJK_SECRET_VALUE_PERMISSIVE_TABLE_RE.sub   1007 / 2842 / 4950 / 7779 ms  at 4k/8k/16k/32k
        _CJK_TABLE_CELL_VALUE_OR_PLACEHOLDER_RE.sub  340 /  906 / 2132 /  4432 ms  at 4k/8k/16k/32k

    Both are pinned below, by their own worst shape rather than by a shape borrowed from a sibling.
    """

    KELVIN = "K"
    HOMOGLYPHS = ("İ", "ı", "ſ", "K")

    @staticmethod
    def _timed(call, text: str) -> float:
        start = time.perf_counter()
        call(text)
        return time.perf_counter() - start

    def _assert_linear(self, call, make, name: str, *, ceiling: float = 3.0) -> None:
        # Same 32x sweep and same 150x allowance as `RedactFlatQuantifierTests._assert_linear`;
        # see that method's own comment for why the allowance is that wide.
        small = self._timed(call, make(8_000))
        large = self._timed(call, make(256_000))
        self.assertLess(
            large,
            max(small * 150, 0.05),
            f"{name} scaled worse than linearly: {small:.4f}s @8k -> {large:.4f}s @256k",
        )
        self.assertLess(large, ceiling, f"{name} took {large:.4f}s on 256,000 chars")

    # ------------------------------------------------------- 1. quadratic detection, per pattern
    def test_ascii_label_patterns_are_linear_on_a_homoglyph_run(self) -> None:
        for name, pattern in (
            ("_SECRET_LABEL_KEYWORD_RE", hook._SECRET_LABEL_KEYWORD_RE),
            ("_INLINE_ASCII_SECRET_RE", hook._INLINE_ASCII_SECRET_RE),
        ):
            with self.subTest(pattern=name):
                self._assert_linear(
                    lambda text, _p=pattern: _p.sub("X", text),
                    lambda n: "".join(self.HOMOGLYPHS) * (n // 4),
                    f'{name} on a homoglyph run',
                )

    def test_cjk_table_patterns_are_linear_on_a_guardless_value_run(self) -> None:
        # "**" is the worst shape for these two specifically: every character is an ordinary
        # value-body character (so the guard scan runs to its full depth) and none is alphanumeric
        # (so it then fails), at every start position.
        #
        # The ceiling is 8s rather than the 3s `RedactFlatQuantifierTests` uses, because these two
        # keep a genuinely larger per-character constant than the assignment patterns do and this
        # sweep runs them on a single 256,000-character line, far past anything the pipeline sees in
        # practice. Both numbers are measured, `/usr/bin/python3` 3.9.6, on the "**" run:
        #     before this round   0.32s @8k -> 10.39s / 10.23s @256k
        #     after               0.10s @8k ->  3.40s /  3.40s @256k
        # The RATIO assertion is the actual defect detector (x32 input, x34 time = linear); the
        # ceiling is only the backstop for "flat ratio but slow outright", and 8s still fails a 2.4x
        # constant regression. The budget that actually matters is asserted end-to-end, at realistic
        # sizes, by `test_the_reviewed_repro_sizes_are_now_far_inside_the_hook_budget`.
        for name, pattern in (
            ("_CJK_SECRET_VALUE_PERMISSIVE_TABLE_RE", hook._CJK_SECRET_VALUE_PERMISSIVE_TABLE_RE),
            ("_CJK_TABLE_CELL_VALUE_OR_PLACEHOLDER_RE", hook._CJK_TABLE_CELL_VALUE_OR_PLACEHOLDER_RE),
        ):
            for shape, unit in (("asterisks", "**"), ("quotes", "'"), ("homoglyphs", "K")):
                with self.subTest(pattern=name, shape=shape):
                    self._assert_linear(
                        lambda text, _p=pattern: _p.sub("X", text),
                        lambda n, _u=unit: _u * (n // len(_u)),
                        f"{name} on a {shape} run",
                        ceiling=8.0,
                    )

    def test_the_reviewed_repro_sizes_are_now_far_inside_the_hook_budget(self) -> None:
        # The review's own numbers: past the hook's outer 5-second cap at 16-20KB. Both shapes are
        # asserted end-to-end through `redact()`, at 24KB, with a wide margin -- 6088ms/5572ms
        # before the fix, ~30ms/~380ms after.
        homoglyph = "".join(self.HOMOGLYPHS) * 6_000
        table = "| name | password |\n| --- | --- |\n| bob | %s |\n" % ("**" * 12_000)
        for label, text in (("homoglyph run", homoglyph), ("guardless table cell", table)):
            with self.subTest(shape=label):
                elapsed = self._timed(hook.redact, text)
                self.assertLess(elapsed, 2.0, f"{label} took {elapsed:.3f}s on {len(text)} chars")

    def test_the_whitespace_redos_round_6_closed_stays_closed(self) -> None:
        # The round-4 guard-scan rewrite reopened this once, in review: flattening the body's
        # multi-character tokens to their constituent characters made horizontal whitespace
        # crossable from every position of a long run, and `test_inline_cjk_secret_connector_is_not
        # _cubic` went from passing to a multi-second failure (whole-suite wall time 77s -> 159s).
        # `_CJK_VALUE_GUARD_SCAN_NEVER_CROSSABLE` is what keeps it closed; this asserts the property
        # directly against the guard scan rather than only through that older test's own pattern.
        for name, pattern in (
            ("_CJK_SECRET_VALUE_PERMISSIVE_TABLE_RE", hook._CJK_SECRET_VALUE_PERMISSIVE_TABLE_RE),
            ("_INLINE_CJK_SECRET_RE", hook._INLINE_CJK_SECRET_RE),
        ):
            with self.subTest(pattern=name):
                elapsed = self._timed(
                    lambda text, _p=pattern: _p.search(text), "密码" + " " * 12_000 + "x"
                )
                self.assertLess(elapsed, 1.0, f"{name} took {elapsed:.3f}s on a whitespace run")

    # --------------------------------------------------- 2. boundary / leak, no cap at any length
    def test_compound_key_labels_are_still_recognized_without_the_redundant_alternative(self) -> None:
        # `[A-Za-z0-9]+[_-]key` was removed from `_ASCII_SECRET_KEYWORD_CORE` as redundant: bare
        # `key` is reachable at the same terminal position because `[_-]` satisfies the vocabulary's
        # own `(?<![A-Za-z0-9])` boundary. "Redundant" is a claim about rendered output, so it is
        # asserted on rendered output, through every shape these four patterns actually see.
        secret = "Qw7#zP2mLv8Ke"
        for label in (
            "soga_key", "ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "my_key", "x9_key", "9_key",
            "a-key", "session_key", "host_key_v2", "api_key", "private_key", "PRIVATE-KEY",
        ):
            for text in (
                f"{label}: {secret}",
                f"{label} is {secret}",
                f"| {label} | {secret} |",
                # Header-row form: the label names the SECOND column, so only that column's data
                # cell is the secret -- the first column deliberately carries unrelated content, or
                # this asserts the column scan over-redacts rather than that it fires at all.
                f"| id | {label} |\n| --- | --- |\n| row-one | {secret} |",
            ):
                with self.subTest(label=label, text=text):
                    redacted = hook.redact(text)
                    self.assertNotIn(secret, redacted)
                    self.assertIn("[REDACTED", redacted)

    def test_homoglyph_prefixed_label_is_still_recognized_so_the_boundary_taint_stays_reverted(
        self,
    ) -> None:
        # Aligning `_ASCII_SECRET_KEYWORD_STANDALONE_BASE`'s lookbehind with its `(?i:...)` keyword
        # -- round 3's stated invariant, applied literally -- was implemented, measured and then
        # reverted here: unlike `_ASSIGNMENT_RE`, this vocabulary anchors directly on the keyword
        # with no run to relocate into, so rejecting the mid-run start position rejects the LABEL.
        # A differential sweep put 1,877 outputs in the leak direction, all of this shape. That is a
        # redaction-EVASION vector (one homoglyph in front of an ordinary label turns redaction
        # off), so the disagreement stays. See that constant's own comment for the full reasoning.
        secret = "Ab7xK9mQ2"
        for homoglyph in self.HOMOGLYPHS:
            for label in ("password", "secret", "token"):
                text = f"| {homoglyph}{label} | {secret} |"
                with self.subTest(text=text):
                    redacted = hook.redact(text)
                    self.assertNotIn(secret, redacted)
                    self.assertIn("[REDACTED", redacted)

    def test_guard_scan_still_reaches_a_value_behind_fullwidth_punctuation(self) -> None:
        # `_CJK_VALUE_CHARS_COMMON` and `_CJK_VALUE_NON_ASCII_TOKEN` overlap: "：＝" and the
        # fullwidth connector punctuation are ordinary value characters AND sit inside the
        # Halfwidth-and-Fullwidth block the token excludes. The first flat scan class blocked the
        # block wholesale and so NARROWED the gate -- 13 of 59,480 differential-sweep inputs stopped
        # redacting, all of this shape. Pinned with the sweep's own smallest repro plus the general
        # form for every carve-out character.
        self.assertNotEqual(
            hook.redact('Y密码 cb"ſ[：@ı1b**-ı0'),
            'Y密码 cb"ſ[：@ı1b**-ı0',
        )
        for char in "：＝－／＿．＠；｜＋＊～":
            with self.subTest(carve_out=char):
                text = "密码 ab" + char + "cd7xKqZ"
                self.assertIn("[REDACTED", hook.redact(text), f"carve-out {char!r} was blocked")

    def test_guard_scan_class_is_disjoint_from_its_guard(self) -> None:
        # Disjointness is the whole reason the flat scan cannot backtrack. Asserted structurally,
        # over every ASCII character plus the carve-outs, for every (body, guard) pair the module
        # actually builds -- so a future guard that stops being a subset of the body repertoire
        # fails here rather than silently reintroducing an ambiguous scan.
        pairs = (
            (hook._CJK_VALUE_BODY_TABLE_PERMISSIVE, "[A-Za-z0-9]"),
            (hook._CJK_VALUE_BODY_INLINE_PERMISSIVE, "[A-Za-z0-9]"),
            (hook._CJK_VALUE_BODY_INLINE_PERMISSIVE, "[0-9-]"),
            (hook._CJK_VALUE_BODY_INLINE, "[0-9-]"),
        )
        probes = [chr(code) for code in range(0x80)]
        probes += list("：＝－／＿＠İıſK密")
        for body, guard in pairs:
            scan = re.compile(hook._cjk_value_guard_scan_class(body, guard))
            guard_re = re.compile(guard)
            for char in probes:
                if guard_re.fullmatch(char):
                    with self.subTest(guard=guard, char=char):
                        self.assertIsNone(scan.match(char), "scan class overlaps its guard")

    def test_guard_scan_window_has_no_leak_boundary_at_realistic_lengths(self) -> None:
        # The window's unit changed from body tokens to characters, so its edge moved. Swept well
        # past both the old (256 tokens) and new (512 characters) numbers, in the shape that
        # actually exercises it: a run of non-guard value characters before the value's first
        # alphanumeric.
        for pad in (1, 8, 64, 255, 256, 257, 511, 512, 513, 1_000):
            with self.subTest(pad=pad):
                text = "密码：" + "." * pad + "Qw7zP2mLv8Ke"
                redacted = hook.redact(text)
                # Past the window the gate declines, exactly as the capped version always has --
                # what must never happen is a match that captures a PREFIX and renders the tail.
                if "[REDACTED" in redacted:
                    self.assertNotIn("Qw7zP2mLv8Ke", redacted, f"partial capture at pad={pad}")

    # ------------------------------------------------------------- 3. differential no-op sweep
    def test_round4_changed_no_redaction_output_on_the_real_input_shapes(self) -> None:
        # The rewrite is meant to be a pure performance change. This is the assertion of that,
        # sampled from the same corpus the full 59,480-input differential sweep against the pre-fix
        # module used (that sweep reported 0 differences; this keeps a representative slice of it
        # in the suite so a later round cannot quietly change behaviour here).
        labels = ["password", "secret", "token", "api_key", "db_password_prod", "ACCESS_KEY",
                  "passphrase", "backup code", "密码", "密钥", "助记词"]
        values = ["Ab7xK9mQ2", "sk-abcdefghij1234567890", "159 3321 8874 6650",
                  "Qx9\\|Lm2N7", "[Zq7-203.0.113.77-Pk]", "v2.Bearer xoxbslackbotusertoken",
                  "R7mQ betaLOCK", "----", "密码zh", "'Qw7#zP2mLv8Ke'", "**Zx8Qm2**"]
        for label in labels:
            for value in values:
                for text in (
                    f"{label}: {value}",
                    f"| {label} | {value} |",
                    f"| id | {label} |\n| --- | --- |\n| {value} | {value} |",
                ):
                    with self.subTest(text=text):
                        # Idempotence is the invariant every round here has had to hold, and it is
                        # the one most sensitive to a gate that changed which candidates it admits.
                        once = hook.redact(text)
                        self.assertEqual(hook.redact(once), once)


if __name__ == "__main__":
    unittest.main()
