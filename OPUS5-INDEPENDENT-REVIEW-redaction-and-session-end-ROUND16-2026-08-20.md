# OPUS5 Independent Review — Round 16

**Scope:** the two follow-up fixes in `claude-codex-memory-bridge/`
**Interpreter:** `/usr/bin/python3` 3.9.6 (`Clang 21.0.0`) — confirmed, Homebrew 3.14.6 not used
**Date:** 2026-08-20
**Mode:** read-only; all verification done against isolated scratch copies in the session scratchpad. No repo file modified.

---

## 0. Verdicts

| Fix | Verdict |
|---|---|
| **Fix 1** — lookaround swap on `_URL_USERINFO_RE`/`_BEARER_RE`/`_TOKEN_RE`/`_JWT_RE`/`_EMAIL_RE` | **GO, with reservations.** The fix is real, verified, and introduces no new leak. But one concrete residual leak survives *inside the newly-fixed set* (P2-1) and a genuine 6th instance of the same bug class was found untouched (P2-2). No P0/P1. |
| **Fix 2** — `elif event != DEFAULT_HOOK_EVENT` fail-closed gate | **GO.** Independently reproduced the pre-fix regression and confirmed the fix closes it on all six call shapes. One latent P2 (P2-4). No P0/P1. |

Recommendation: both fixes are sound enough to keep. Because this exact bug class has now recurred **three** times in this file, I would fold P2-1 and P2-2 into this round rather than ship a fourth recurrence — both have validated one-line fixes (§5).

---

## 1. Claim verification (against file:line, not the summary)

All five patterns are where claimed and read as claimed:

- `_URL_USERINFO_RE` — `claude_memory_hook.py:874`
- `_BEARER_RE` — `:875`
- `_TOKEN_RE` — `:876-879`
- `_JWT_RE` — `:891-893`
- `_EMAIL_RE` — `:1120-1123`, `re.ASCII` removed, flags now exactly `re.IGNORECASE` (verified: `flags == 34`, identical to the original `(?i)` form)
- `install_bridge.py:1012-1039` — `elif event != DEFAULT_HOOK_EVENT:` at `:1020`, `raise InstallError(f"missing {event} hook list")` at `:1035`

Test suite: **`Ran 232 tests ... OK`** on `/usr/bin/python3` 3.9.6 (226 prior + 6 new). Confirmed.

Git scope in `完善orca/` for the bridge directory is exactly as expected:

```
 M claude-codex-memory-bridge/claude_memory_hook.py
 M claude-codex-memory-bridge/install_bridge.py
 M claude-codex-memory-bridge/tests/test_claude_memory_hook.py
 M claude-codex-memory-bridge/tests/test_install_bridge.py
?? claude-codex-memory-bridge/AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md
?? claude-codex-memory-bridge/tests/test_nontrigger_corpus.py
?? claude-codex-memory-bridge/tests/test_write_candidate_capture.py
?? claude-codex-memory-bridge/write_candidate_capture.py
```

`prime-agent-integration/` is separately modified by a concurrent session. `grep -rn "prime-agent|prime_agent|install_prime" claude-codex-memory-bridge/` returns **zero hits** — no bridge file references or depends on it. Not a scope violation of this candidate.

---

## 2. Fix 1 — what is genuinely correct

**(a) The claimed leaks are real and are closed.** I built my own scratch copy of `claude_memory_hook.py` with *only* the five patterns reverted to their pre-fix `\b` forms, and ran 12 vectors through the real `redact()` on both:

| vector | pre-fix | post-fix |
|---|---|---|
| `资料user@example.com学习` | LEAK | redacted |
| `开头无空格user@example.com` | LEAK | redacted |
| `user@example.com结尾无空格` | LEAK | redacted |
| `授权Bearer abcdefgh12345678令牌` | LEAK | redacted |
| `密钥sk-proj-abcdefgh12345678泄露` | LEAK | redacted |
| `sk-proj-abcdefgh12345678泄露了` | LEAK | redacted |
| `AKIAABCDEFGHIJKLMNOP泄露了` | LEAK | redacted |
| `令牌<JWT>泄露` | LEAK | redacted |
| `<JWT>泄露了` | LEAK | redacted |
| `访问https://user:supersecretpw@host今天` (dotless host) | LEAK | redacted |

10/10 genuine reproductions. The two U+212A homoglyph vectors correctly show "not a leak" against *this* revert, because my revert restores the **original** `\b`+`(?i)` form, under which the homoglyph always matched. The regression they pin belongs to the **interim `re.ASCII`** form, which I reproduced separately: under `re.ASCII` all four homoglyph chars LEAK, under the original `\b` they redact, and under the new lookaround form they redact. So the homoglyph regression is genuinely introduced-and-closed.

**(b) The swap can never cause a new leak.** 1,000,000 differential fuzz cases (5 patterns × 200k, neighbors drawn from ASCII punctuation, letters, digits, CJK, Greek, accented Latin, long-s, ZWSP, full-width digits, CJK punctuation): **zero** cases where the new pattern covers fewer characters than the old one. Every divergence is strictly superset. This is the property that matters for a redactor, and it holds.

**(c) IGNORECASE is fully restored, not just for the one example.** I enumerated the entire Unicode range: exactly four non-ASCII characters are matched by `[A-Z]` under Unicode `IGNORECASE` but not under `re.ASCII` — U+0130 `İ`, U+0131 `ı`, U+017F `ſ`, U+212A `K`. All four redact correctly through the real `redact()` in all three positions (TLD, domain start, local part). The claim "restores full IGNORECASE" is complete, not cherry-picked.

---

## 3. Fix 1 — findings

### P2-1 (NEW, inside the newly-fixed set) — `_BEARER_RE:875`: the new lookaround is itself IGNORECASE-tainted

`_BEARER_RE` carries `(?i)`, which applies to the **lookbehind's character class too**. Under `IGNORECASE`, `[A-Za-z0-9_]` also matches U+0130, U+0131, U+017F, U+212A. So the boundary guard treats those four as word characters and fails — exactly the failure shape the fix set out to eliminate.

Reproduced through the real `redact()`:

```
redact("İBearer abcdefgh12345678")  ->  'İBearer abcdefgh12345678'   # unchanged, full token leaked
redact("ıBearer abcdefgh12345678")  ->  'ıBearer abcdefgh12345678'
redact("ſBearer abcdefgh12345678")  ->  'ſBearer abcdefgh12345678'
redact("KBearer abcdefgh12345678") -> unchanged
```

`_URL_USERINFO_RE` and `_EMAIL_RE` carry the same taint but are **accidentally immune**: their leading character classes (`[a-z]`, `[A-Z0-9._%+-]`) also match those four chars under IGNORECASE, so the match simply starts one character earlier and the credential still redacts. `_TOKEN_RE` and `_JWT_RE` have no IGNORECASE and are clean. Only `_BEARER_RE` — whose leading element is the literal `Bearer` — cannot absorb the preceding character.

**Severity P2, not P1:** impact is high (a full bearer token in plaintext into an LLM context) but likelihood is low. The CJK version was P1 because CJK has no inter-word spaces, so gluing is the *natural* way the text occurs. Turkish `ı`/`İ` and long-s occur in space-separated orthographies, so zero-separator gluing is contrived.

**The durable risk is larger than the instance.** The fix's comment now presents this lookaround as the file's established idiom, and `_ASSIGNMENT_RE` and `_QUERY_SECRET_RE` also carry IGNORECASE. The next pattern added with this idiom + `(?i)` silently inherits the same hole.

### P2-2 (the 6th pattern, same bug class, untouched) — `_IPV4_RE:981-984`: `(?<![\d.])` uses Unicode-aware `\d`

This is the direct analogue of the `\b` bug — a Unicode-aware shorthand used as a boundary guard — in the one redaction pattern the fix did not touch. Every non-ASCII decimal digit suppresses IPv4 redaction entirely when it immediately precedes the address. All eight tested scripts leak:

```
'０10.11.12.13'  (U+FF10 fullwidth)      -> unchanged, address leaked
'٣10.11.12.13'  (U+0663 Arabic-Indic)   -> unchanged
'๙10.11.12.13'  (U+0E59 Thai)           -> unchanged
'۵','०','০','᱐','𝟎' (U+06F5/0966/09E6/1C50/1D7CE) -> all unchanged
```

Trailing side is safe (backtracking recovers). The same Unicode `\d` in the pattern **body** also produces a mirror false positive that destroys real text: `版本２.３.４.５发布` → `版本[REDACTED_IP]发布` (a full-width version number redacted as an IP).

### P2-3 (pre-existing; trigger surface widened by this working tree) — CJK over-capture silently deletes memory content

Chinese/Japanese have no inter-word spaces, so every value class that relies on whitespace to terminate swallows the entire following CJK clause and deletes it. Not a leak — a memory-content integrity loss, in a bridge whose primary user writes Chinese:

```
_ASSIGNMENT_RE:953   'api_key=abc123然后我们继续讨论下一个话题吧' -> 'api_key=[REDACTED]'
_QUERY_SECRET_RE:964 '...?token=abc123然后继续说明这个接口'      -> '...?token=[REDACTED]'
_HOME_RE:1125        '备份到 /Users/alice的目录下面'              -> '备份到 $USER_HOME'
_IPV6_CANDIDATE_RE:1070 'fe80::1%eth0然后重启网络'               -> '[REDACTED_IP]'
```

The `_ASSIGNMENT_RE` change present in this same working tree (`[A-Za-z0-9]+[_-]key`, `:948`) widens the trigger surface: `primary_key=id然后我们给这张表建立索引提升查询速度` → `primary_key=[REDACTED]`. Also confirmed the comment's acknowledged tradeoff is live: `low-key=true` and `hot-key=ctrl` now redact, while the guarded `turkey=`/`monkey=` correctly do not.

### P3 (comment accuracy — matters because it is the recurrence mechanism) — `claude_memory_hook.py:868-871`

The fix's own comment asserts two invariants, **both empirically false**:

> "reproduces `\b`'s prior behavior byte-for-byte on every pure-ASCII boundary decision"

False. 6,144 pure-ASCII divergences found. Concretely:
- `_TOKEN_RE`/`_JWT_RE` now include a trailing `-` that `\b` excluded: `sk-proj-ABCDEFGH1234-` → old span `(0,20)`, new span `(0,21)`.
- `_EMAIL_RE` now starts one char earlier at a leading `.`/`-`/`%`/`+`: `-user@example.com` → old span `(1,17)`, new span `(0,17)`.

All divergences are in the safe (superset) direction, so no leak — but the invariant as written is not what the code does.

> "making CJK (and every other non-ASCII 'word' character) count as non-word on both sides"

False for the four characters in P2-1 on the three IGNORECASE patterns.

The same false claim appears in the new test at `tests/test_claude_memory_hook.py:~233` ("the fix must not change any pure-ASCII boundary decision"). Given this bug class recurred three times precisely because a boundary's actual semantics were assumed rather than checked, leaving a stated-but-false invariant in the file is a real hazard, not a nit.

---

## 4. Fix 2 — verification and findings

**Confirmed from source via AST walk, not from the claim.** There are exactly two `update_hook_config` call sites in the module:

```
L1668  in install    update_hook_config(raw, release['command'])
L2069  in plan       update_hook_config(raw, release['command'])
```

Both are two positional args — no `event=`, no `remove=`. So the gate protects the real call path. `_owned_shape_match` / `_attempt_structural_detection` / `_contains_owned_handler` all thread `event` through defaults only (`_find_untracked_owned_configs:981` passes no `event=`). An AST scan for assignments to `DEFAULT_HOOK_EVENT` finds only the module-level definition at `:39`.

**Independently reproduced the regression.** I built my own scratch copy with *only* the `elif`/`else` split reverted to the unconditional pre-fix `else: event_handlers = []`, and ran six call shapes against both:

| call shape | pre-fix | post-fix |
|---|---|---|
| zero non-default args, `{"hooks":{"SessionStart":[]}}` | silently created key | `InstallError` |
| zero non-default args, `{"hooks":{}}` | silently created key | `InstallError` |
| `event=DEFAULT_HOOK_EVENT` | silently created key | `InstallError` |
| `event="UserPromptSubmit"` raw literal | silently created key | `InstallError` |
| `event="".join(["User","Prompt","Submit"])` (distinct object) | silently created key | `InstallError` |
| `remove=True`, key missing | silently created key | `InstallError` |

**No comparison bug.** The fifth row answers the explicit question: `!=` on `str` is value comparison, so a runtime-built equal string (`a == b` True, `a is b` False) correctly hits the raise branch. There is no identity-comparison hazard.

**Gate does not disturb the path it exists to protect.** `update_hook_config(EMPTY, "cmd", event="SessionEnd")` produces byte-identical output on both the fixed and pre-fix modules.

**Reachability confirmed.** `_enumerate_hook_configs()` (`install_bridge.py:334-364`) appends every `codex-accounts/*/home/hooks.json` that is a regular file, with no check for the `UserPromptSubmit` key. A config like `{"hooks":{"SessionStart":[]}}` genuinely reaches `update_hook_config` at `:1668` and now aborts install. The downstream `.get(DEFAULT_HOOK_EVENT, [])` reads at `:1713` (install) and `:1911` (verify) also fail closed on a missing key.

**Error-message compatibility.** The absent-key message is byte-identical to the original pre-section-13 text (`"missing UserPromptSubmit hook list"`). The new `"malformed {event} hook list"` text applies only to the present-but-wrong-type case, and no test or caller matches on message text.

### P2-4 (latent) — def-time / call-time asymmetry in the gate

`event: str = DEFAULT_HOOK_EVENT` (`:1006`) snapshots the string at **function-definition** time; `elif event != DEFAULT_HOOK_EVENT` (`:1020`) resolves the module global at **call** time. If anything ever rebinds `install_bridge.DEFAULT_HOOK_EVENT`, the two desynchronize and the fail-closed guard **silently inverts** — a zero-arg `install()`-shaped call takes the auto-init branch and creates the key instead of raising. Demonstrated on a scratch copy:

```
scr.DEFAULT_HOOK_EVENT = "SomethingElse"
scr.update_hook_config(EMPTY, "cmd")   # zero-arg, exactly install()'s shape
-> NO RAISE; returns a payload with "UserPromptSubmit" created
```

Not reachable today: nothing in the module rebinds it, and `test_update_hook_config_default_event_is_unchanged` pins the value. Worth hardening because the failure mode is silent and this is a fail-closed guard.

---

## 5. Validated remediations (all confirmed working on 3.9.6)

**P2-1** — scope IGNORECASE off for the lookaround only:

```python
_BEARER_RE = re.compile(r"(?i)(?-i:(?<![A-Za-z0-9_]))(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
```

Verified: `İ`/`ı`/`ſ`/`U+212A`/CJK/`-`/space/start-of-string all now MATCH, while `a`/`_`/`0` correctly still do **not** match (correct `\b` parity preserved). Apply the same scoping to `_URL_USERINFO_RE` and `_EMAIL_RE` for consistency even though they are accidentally immune, so the idiom is sound wherever it is copied next.

**P2-2** — ASCII-only digit classes in `_IPV4_RE`:

```python
_IPV4_RE = re.compile(
    r"(?<![0-9.])(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(?:\."
    r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}(?![0-9])(?!\.[0-9])"
)
```

Verified: fixes all eight Unicode-digit leaks **and** the `２.３.４.５` false positive, while preserving every existing behavior including the sentence-final-period case (`reachable at 10.0.0.1.` still redacts) and the `1.2.3.4.5` / `999.1.1.1` rejections.

**P2-4** — resolve the default from one place:

```python
def update_hook_config(raw, command, *, event: str | None = None, remove: bool = False):
    if event is None:
        event = DEFAULT_HOOK_EVENT
```

**P3** — correct the two false invariants in `claude_memory_hook.py:868-871` and the matching test comment: state that the lookaround is a **superset** of `\b` on ASCII (never a subset — that is the property that matters), and that under `IGNORECASE` the ASCII class additionally matches U+0130/U+0131/U+017F/U+212A unless scoped with `(?-i:...)`.

---

## 6. Honest bottom line

Both fixes do what they claim, and I verified both against my own independent scratch reverts rather than the summary. Fix 2 is clean — I tried the monkeypatch angle, the string-identity angle, the `remove=True` angle, and the reachability angle, and the only thing I found is a latent fragility nobody can currently trigger.

Fix 1 is correct and strictly safer than what it replaced (proven: one million fuzz cases, zero coverage loss). But it did not fully close its own bug class: the idiom is unsound under `IGNORECASE`, which leaves `_BEARER_RE` leaking after four specific characters, and `_IPV4_RE` is a genuine sixth instance that was never examined. Neither rises to P0/P1 on honest likelihood grounds, so neither blocks. Given three prior recurrences, though, the cheap move is to close both now — the two one-line fixes above are already validated.
