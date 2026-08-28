# OPUS5 Independent Adversarial Review — 2026-08-20

**Scope:** two uncommitted changes in `claude-codex-memory-bridge/`
**Method:** read-only. Every claim re-verified by independent reproduction against the committed
`HEAD` version, not by trusting the new tests.
**Interpreter:** `/usr/bin/python3` = **Python 3.9.6** (Xcode-pinned), confirmed.
**Suite:** `Ran 226 tests ... OK`, re-run twice, stable. Bridge file hashes unchanged across the review.

Verdicts: **Change 1 = GO** (with one mandatory tracked P1 follow-up). **Change 2 = NO-GO.**

---

## Change 1 — `claude_memory_hook.py` redaction fixes

### Confirmed correct

Both claimed leaks reproduce on committed `HEAD` and are genuinely fixed:

| case | HEAD | working tree |
|---|---|---|
| `资料user@example.com学习` | leaks in full | `资料[REDACTED_EMAIL]学习` |
| `soga_key=abcd1234…` | leaks in full | `soga_key=[REDACTED]` |

- **Pure-ASCII `_EMAIL_RE` behavior is byte-identical.** 300,000 randomized ASCII strings, old
  pattern vs new: **0 mismatches**. The claim holds.
- **Negative guards hold**, and beyond the three claimed: `turkey=5`, `monkey=xyz123`,
  `hockey=`, `donkey=`, `whiskey=`, `jockey=`, `keyboard=`, `keys=`, `key=`, `--key=` — all
  correctly untouched. The `[_-]` requirement does what the comment says.
- **Named compounds still redact**: `api_key`, `password`, `access_token`, `AWS_SECRET_ACCESS_KEY`.
- **`_key` branch strictly widens coverage** — 400,000-case positional-coverage fuzz found exactly
  one non-superset case (P2-D below).
- `re.ASCII` touches only `\b` in this pattern (no other `\w`/`\d`/`\s` present) — plus `IGNORECASE`,
  which is the undocumented part (P2-A).

### P1-A — the fix is incomplete, in the higher-severity direction (pre-existing, NOT introduced)

The identical Unicode-`\b` bug is still live in **three sibling patterns that carry higher-value
credentials than email**, and this change does not touch them:

- `claude_memory_hook.py:859` `_BEARER_RE`
- `claude_memory_hook.py:860-862` `_TOKEN_RE` — `sk-`, `sk-proj-`, `gh[opusr]_`, `github_pat_`, `xox[baprs]-`, `AKIA`, `ASIA`
- `claude_memory_hook.py:872` `_JWT_RE` — both anchors

Reproduced, leaking in full on **both** HEAD and the working tree:

```
密钥sk-abcdefghijklmnopqrst          -> unredacted   (leading CJK)
sk-abcdefghijklmnopqrst密钥          -> unredacted   (trailing CJK)
令牌ghp_abcdefghijklmnopqrstuvwxyz  -> unredacted
凭证AKIAIOSFODNN7EXAMPLE            -> unredacted
令牌Bearer abcdefghijklmnop         -> unredacted
令牌eyJhbGci….eyJzdWIi….SflKxwRJ    -> unredacted   (JWT, both edges)
ключsk-abcdefghijklmnopqrst          -> unredacted   (Cyrillic, same root cause)
```

The realism argument that justified the email fix applies with more force here: this project's own
transcripts are substantially Chinese, and `密钥sk-…` / `令牌ghp_…` is a natural shape.

Near-miss worth noting: `_URL_USERINFO_RE` (`:858`) has the same bare `\b` and also fails to match
when CJK-adjacent — the password survived only because `_EMAIL_RE` incidentally caught the
`user@host.tld`-shaped remainder. With a dotless host (`localhost`, a bare IP) that backstop is gone.

**This is not a defect in the diff** — it is pre-existing. It must not be blocked on, but it must
also not be reported as closed. Shipping "the CJK boundary bug is fixed" while these remain creates
a false closure signal.

**Recommended fix (one idiom, all five patterns):** replace bare `\b` with the explicit ASCII
lookarounds this same file already uses in `_LONG_BLOB_RE`, `_MAC_ADDRESS_RE`, and `_ASSIGNMENT_RE`
— `(?<![A-Za-z0-9_])` / `(?![A-Za-z0-9_])`. That fixes all of them and also eliminates P2-A.

### P2-A — `re.ASCII` also narrows `IGNORECASE`; the in-code comment denies this

`claude_memory_hook.py:1079`. `re.ASCII` scopes case-folding as well as `\b`. Under Unicode
`IGNORECASE`, `[A-Z]` matches U+212A KELVIN SIGN, U+017F LONG S, U+0130, U+0131. Under
`re.ASCII` it does not. Reproduced — **HEAD redacted these, the working tree leaks them in full**:

```
user@example.uK    (U+212A)   HEAD: [REDACTED_EMAIL]   NEW: user@example.uK      <-- new leak
user@eKample.com   (U+212A)   HEAD: [REDACTED_EMAIL]   NEW: user@eKample.com     <-- new leak
uſer@example.com   (U+017F)   HEAD: full redact        NEW: partial (er@example.com)
```

The comment at `:1071-1078` states the fix "flips only the CJK-adjacent cases" and is
"byte-identical … on every pure-ASCII input" — the second half is empirically true, the first half
is false, and the comment is load-bearing documentation on a live security function.

Rated **P2, not P1**: the affected inputs are Unicode-cased homoglyphs inside an otherwise
ASCII-shaped address, HEAD's coverage of them was an accident of case folding rather than design,
such an address is not deliverable anyway, and the change's net effect is strongly positive. The
lookaround fix in P1-A removes this entirely.

### P2-B — new false-positive class: hyphenated English prose

`claude_memory_hook.py:924-930`. The comment justifies the `[_-]` requirement as excluding prose
("turkey"/"monkey"/"hockey"), and names only `primary_key=`/`sort_key=` identifiers as the accepted
tradeoff. It misses that ordinary hyphenated English prose **is** separator-joined and now matches:

```
low-key: relaxed vibe          -> low-key: [REDACTED] vibe
off-key: the singing was bad   -> off-key: [REDACTED] singing was bad
turn-key: solution ready       -> turn-key: [REDACTED] ready
hi-key: bright lighting        -> hi-key: [REDACTED] lighting
```

Over-redaction is the safe failure direction and this is not a leak — but it mangles legitimate
MEMORY.md prose, and the comment's stated rationale is incomplete. Recommend widening the comment
rather than the pattern.

### P2-C — match-boundary shift can shadow a following real secret

The new branch can consume a following keyword as its own *value*, so `re.sub`'s scan resumes past
the point where the next assignment would have matched:

```
cache_key=token =s3cr3tRealValue
  HEAD:  cache_key=token =[REDACTED]        <- secret redacted
  NEW:   cache_key=[REDACTED] =s3cr3tRealValue   <- secret SURVIVES
```

Frequency: 1 case in 400,000 randomized strings drawn from a **keyword-enriched** alphabet; rarer
in natural text. Requires the first assignment's value to be exactly a redaction keyword with the
real secret following after whitespace. Layered patterns often still catch it (`deploy_key=secret
=AKIA…` is still caught by `_TOKEN_RE`). Contrived, but real and reproduced.

### P2-D — pre-existing superlinear backtracking, mildly worsened

`_ASSIGNMENT_RE` backtracks superlinearly on separator-dense input. **Pre-existing** — HEAD is
equally superlinear. The new branch adds a consistent **~1.5–2.0x constant factor**, not a
complexity-class change:

| single block | HEAD assignment RE | new assignment RE |
|---|---|---|
| 16,000 chars of `ab-` | 2.88 s | 4.26 s |
| 65,536 chars of `ab-` | — | ~140 s |

Realistic prose is unaffected: 58,400 chars → **16.0 ms vs 17.7 ms (~10%)**.

Flagged only because `redact()` runs inside a hook with a **5 s timeout**, `max_file_bytes` may
reach `HARD_FILE_BYTES = 262_144`, and `redact()` runs per-block on the untruncated buffer — so the
change consumes ~1.5–2x more of a fixed budget on a pathological (self-authored) MEMORY.md. Not
introduced by this change; not a blocker.

### P2-E — undisclosed third change in the same diff

`claude_memory_hook.py:189-197` also relaxes `validate_policy()` to tolerate an optional
`write_trigger` key. The review brief described change 1 as exactly two redaction fixes.

Behavior verified correct and narrow: all required keys still mandatory, other unknown keys still
rejected, base v1 policy validates identically, and the read-side hook never *reads* `write_trigger`
(grep: comment + `optional_keys` only). `write_trigger` of any type is accepted, with shape
validation deferred to `write_candidate_capture.py` as the comment states. No risk on the read path
— but it is scope that arrived unannounced.

---

## Change 2 — `install_bridge.py` SessionEnd capability

### Confirmed correct

- `install()` → `update_hook_config(raw, release["command"])` at **`:1659`**, `plan()` at **`:2060`**
  — **zero non-default args**, verified in source, not merely claimed.
- `install()` `:1704` and `verify()` `:1902` use the `DEFAULT_HOOK_EVENT` constant; value identical.
- `uninstall()` never calls `update_hook_config` at all — it restores from backups. `remove=True`
  has **zero production call sites** (grep), so that path is test-only.
- `event` is threaded correctly end to end through `_owned_shape_match` (`:527`),
  `_attempt_structural_detection` (`:566`), `_contains_owned_handler` (`:610`).
- Defaults reproduce prior values: `DEFAULT_HOOK_EVENT="UserPromptSubmit"`, `DEFAULT_HOOK_TIMEOUT=5`.
- No SessionEnd registration exists anywhere in production code — comments only. The "capability
  only" framing is accurate.
- SessionEnd registration preserves other event keys (verified independently).
- No lint config in the tree; the widened `:566` signature (113 chars) is not even the longest line.

### P1 — the "zero regression" claim is falsified: fail-closed became silent mutation

`install_bridge.py:1002-1032`. The presence-check rewrite changed behavior **on the default event**,
via the exact zero-arg call `install()` and `plan()` make. Reproduced against committed `HEAD`:

| input `hooks.json` | HEAD | working tree |
|---|---|---|
| `{"hooks":{}}` | `InstallError: missing UserPromptSubmit hook list` | **silently creates the key and installs** |
| `{"hooks":{"SessionStart":[…]}}` | `InstallError: missing UserPromptSubmit hook list` | **silently adds `UserPromptSubmit`** |
| `{"hooks":{"UserPromptSubmit":"x"}}` | `InstallError: missing …` | `InstallError: malformed …` (message changed) |

**Reachability.** `_enumerate_hook_configs()` (`:334-363`) adds `~/.codex/hooks.json`
*unconditionally* and every `codex-accounts/*/home/hooks.json` regular file. `install()` then calls
`validate_owned_file()` → `update_hook_config()` on each. A newly added Codex account home whose
`hooks.json` is `{"hooks":{}}` or carries only `SessionStart` hooks — an ordinary shape for a fresh
Codex home — reaches this directly.

**Concrete failure scenario.** Operator adds `codex-accounts/acct-three/`, whose `hooks.json` has
only `SessionStart` hooks. Before: `plan`/`install` fail loudly, forcing the operator to look at
that file. After: the installer silently creates a `UserPromptSubmit` key in a file that never had
one and registers into it, with no prompt and no distinct signal.

**The in-code comment actively misleads.** `:1019-1027` asserts this branch "is unreachable for that
event on any already-bridged or freshly-discovered live config today, so this change is inert for
every existing UserPromptSubmit install." The *already-bridged* half is true; the
**freshly-discovered half is false**, and that is exactly the case the branch was added for.

**Why the stated verification method missed it.** The `git stash` + re-run-the-suite methodology can
only prove you did not break what was already tested. **No test pinned the old raise** for a
default-event config missing the key (the nearest, `:71`, uses `{"hooks":{},"extra":1}` and trips
the *structure* check instead), and the new tests exercise the missing-key path only under
`event="SessionEnd"`. So "same 214 tests pass both times" was never capable of detecting this.

**Fix (small).** Gate the auto-initialize on the non-default event, preserving fail-closed for the
event that is actually live:

```python
elif event != DEFAULT_HOOK_EVENT:
    event_handlers = []
else:
    raise InstallError(f"missing {event} hook list")
```

…or add an explicit `allow_create_event: bool = False` opt-in. Then add a test pinning the
default-event raise, so the guard cannot silently regress again.

Rated **P1, not P0**: it adds a key rather than destroying data, other events are preserved
(verified), the resulting file is valid, `verify()` still passes, and `uninstall()` restores from
backups taken before the write. But it converts a hard guard into a silent write in an installer
whose entire review history is about not silently mutating files it does not fully understand — and
the change's whole premise is "no new default behavior."

### P2 — error-message change

`"missing UserPromptSubmit hook list"` → `"malformed UserPromptSubmit hook list"`. Nothing in code
or tests depends on the string (grepped), so impact is operator-facing only. Noted because,
combined with the P1, two previously-identical failure modes are now split: one raises with a new
message, the other no longer raises at all.

---

## Working-tree check

`claude-codex-memory-bridge/` is **exactly** as described — 4 modified files, plus
`write_candidate_capture.py`, `tests/test_write_candidate_capture.py`,
`tests/test_nontrigger_corpus.py`, `AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md` untracked. Nothing else.

**Deviation:** the brief said "nothing else, especially not `prime-agent-integration/`". Two tracked
files there **are** modified:

```
 M prime-agent-integration/install_prime_agent.py        (+421 lines)
 M prime-agent-integration/tests/test_install_prime_agent.py
```

They belong to the concurrent prime-agent round-37/38 workstream (see `HEAD` commits), not to either
change under review. **The tree was not quiescent during this review** — the second file appeared as
modified partway through the session. The bridge files themselves were hash-stable throughout, so
these findings stand, but a parallel reviewer may observe a different tree outside the bridge dir.

---

## Verdicts

### Change 1 — `claude_memory_hook.py` redaction fixes: **GO**

Both fixes are real, reproduce on HEAD, are correctly fixed, and are a clear net security
improvement. No P0/P1 is *introduced* by the diff. The four P2s are all either safe-direction
(over-redaction), exotic (homoglyph), contrived (boundary shift), or pre-existing (backtracking).

**Mandatory condition:** P1-A must be filed as a named, open defect. This change must not be
described as closing the CJK boundary class while `_TOKEN_RE`, `_JWT_RE`, `_BEARER_RE`, and
`_URL_USERINFO_RE` still leak `sk-`/`ghp_`/`AKIA`/JWT/Bearer credentials on CJK adjacency. Applying
the explicit-ASCII-lookaround idiom to all five patterns closes P1-A and P2-A together and is the
recommended follow-up.

### Change 2 — `install_bridge.py` SessionEnd capability: **NO-GO**

The change is well-structured and the parameterization is threaded correctly, but its central
claim — "purely additive, no new default behavior; a plain install today does exactly what it did
before" — is falsified by a reproduced, reachable default-event behavior change from fail-closed to
silent file mutation, which the in-code comment affirmatively denies. The fix is ~3 lines plus one
test. Re-review the gated version; nothing else in this change needs to move.
