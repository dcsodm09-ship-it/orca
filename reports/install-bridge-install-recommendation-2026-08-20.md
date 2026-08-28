# Recommendation: should `install_bridge.py install` be run now?

Opus 5 / max — independent synthesis pass, 2026-08-20. A parallel Codex synthesis ran
separately with no coordination. **This is advisory input only. It authorizes nothing.**
The install must not run without the user's own separate, explicit go-ahead.

---

## Verdict

**YES — deploy the redaction fix. Run `install_bridge.py install`.**

Two things must be true at the moment you run it (both cheap, see §5):

1. You are willing and able to interactively re-trust hooks for **3 Codex accounts**
   immediately afterward.
2. You are not, in that same window, depending on Orca's live Codex pane status in the
   two managed sub-accounts.

Separately and explicitly: **nothing write-trigger / SessionEnd related should be
activated** — and it is not on the table regardless. Verified below, not assumed.

---

## 1. Scope check — independently verified, my reading AGREES with the brief

I did not take the summary on trust. Five independent lines of evidence:

**a. There is no syntactic way to pass a non-default event.**
`install()` is declared `def install() -> dict[str, Any]:` — it takes **zero parameters**.
The CLI (`main()`, `install_bridge.py:2284`) declares exactly one positional argument:
`action`, `choices=("plan","install","verify","uninstall","recover")`. There is **no
`--event`, no `--bridge-id`, no `--timeout` flag**.

**b. Only two non-test call sites of `update_hook_config()` exist**, and both pass zero
keyword arguments:
- `install_bridge.py:1808` (inside `install()`): `update_hook_config(raw, release["command"])`
- `install_bridge.py:2209` (inside `plan()`): `update_hook_config(raw, release["command"])`

Every single `event="SessionEnd"` occurrence in the repository lives in
`tests/test_install_bridge.py`. The only mention in `write_candidate_capture.py` is a
comment.

**c. `write_candidate_capture.py` is never deployed.** `make_release()` ships exactly two
files: `claude_memory_hook.py` and `policy.json`. `write_candidate_capture.py` is not
copied, not referenced by the hook command, and not named anywhere in the release payload.
The command hardcodes `--bridge-id BRIDGE_ID` (the default constant).

**d. Byte-level proof on the three real config files.** I computed the exact before/after
content that install would write:

| file | events before | events after | `SessionEnd` after? | `write_candidate` after? |
|---|---|---|---|---|
| `.codex/hooks.json` | SessionStart, UserPromptSubmit | *identical* | No | No |
| `codex-accounts/0b4cd443…/home/hooks.json` | 8 events | *identical* | No | No |
| `codex-accounts/b9f32a51…/home/hooks.json` | 8 events | *identical* | No | No |

**The entire diff is one line per file** — the release hash inside the bridge command path:
`…/releases/bb8f8eb2…/claude_memory_hook.py` → `…/releases/1fa909cd…/claude_memory_hook.py`.
Nothing else changes.

**e. The deployed `policy.json` is unchanged except `runtime_root`.** No `write_trigger`
key is emitted. Confirmed field-by-field against the currently-installed policy.

**Conclusion: install deploys the redaction fix and nothing else. The write-trigger
capability stays 100% inert.** The brief's characterization is correct.

---

## 2. Bonus verification — the whole `install_bridge.py` delta is inert at defaults

`install_bridge.py` changed by +308 lines. Rather than reason about it, I ran a
**differential test of git HEAD (the version that produced the current, live install)
against the working tree**, at default parameters, on the three real `hooks.json` files:

| check | result |
|---|---|
| `_contains_owned_handler()` on all 3 configs | identical (True/True/True) |
| `update_hook_config()` output on all 3 configs | **byte-identical** |
| `_find_untracked_owned_configs()` with real receipt paths | identical (`[]` / `[]`) |

**ALL DEFAULT-PARAM BEHAVIOR IDENTICAL: True.**

This matters because `install()` *does* reach the round-19-fixed code path (it calls
`recover_pending_install()`, which calls `_find_untracked_owned_configs()`). The fix is
pure parameter threading whose defaults are the same constants that were previously
hardcoded — so it is provably a no-op for the install path.

---

## 3. The case FOR is stronger than the brief claims

**Production really is running the old, buggy code — confirmed by hash, not by report:**

- installed script SHA256 `8856a9d2…` — **exactly equal to git HEAD**
- fixed working-tree script `3a02bb06…` — exists nowhere in production

**The leak is real and I reproduced it myself** on the currently-live script, using
synthetic/fake secrets in the CJK-adjacent shape:

| case (CJK-adjacent, no separator) | OLD (live) | NEW (fixed) |
|---|---|---|
| email | **fully unredacted** | `[REDACTED_EMAIL]` |
| `Bearer <token>` | **fully unredacted** | `[REDACTED]` |
| JWT | **fully unredacted** | `[REDACTED_TOKEN]` |
| `password=…` | redacted | redacted |
| IPv4 | redacted | redacted |
| `token=ghp_…` | redacted, but *swallowed* trailing CJK text | redacted, trailing text preserved |
| URL userinfo | partial (`user:` left visible) | full userinfo redacted |

**3 of 7 pass through completely unredacted on the live hook. 0 of 7 on the fixed one.**

**The severity multiplier the brief understates:** this is a Chinese-language working
environment. CJK adjacency is not an exotic Unicode edge case here — it is the *dominant*
text shape in this user's memory files. The bug's real-world hit rate in this specific
deployment is high, not marginal. This hook injects memory excerpts into every Codex
`UserPromptSubmit` across three accounts.

**And the fix is measurably safe on real data.** I ran the old and new scripts
side-by-side as real hooks over 8 real workspaces × 3 prompts:

> **24 of 24 runs byte-identical. 0 differing.**

That is the ideal risk profile: **no observable change on the real corpus, strictly more
redaction on the leak shapes.** Consistent with round 18's 60k differential fuzz cases.

**End-to-end smoke test passed:** I built a real content-addressed release on the SSD and
invoked the fixed script exactly as Codex invokes it. It emits well-formed
`hookSpecificOutput` context (4174 B / 4530 B / 3211 B on three workspaces) — proving the
fixed script genuinely works, not merely that it fails silently.

---

## 4. Know this before you authorize

**4.1 — The literal latest recorded verdict on `install_bridge.py` is NO-GO, not GO.**
The newest review artifact on disk,
`OPUS5-INDEPENDENT-REVIEW-install-bridge-ROUND15-2026-08-20.md` (18:19), states
*"NO-GO for this fix."* Its P1-A was subsequently fixed (design §19.1) and its P2-A…P2-E
became the 5 documented limitations — **but no confirming re-review was ever run to flip
that NO-GO to GO.** The sub-thread was closed by the user's explicit decision plus
self-verification, not by a fresh dual-GO. The framing "reached final whole-candidate
dual-GO after 3 rounds" is generous to the actual record.
*Why this does not change the recommendation:* §2 above empirically proves the entire
unreviewed delta is behaviorally identical at default parameters on the real files.

**4.2 — Codex-side review artifacts for rounds 15-19 do not exist on disk.** Only
`CODEX-*.md` through ROUND14 are present, though the design doc prose claims parallel
opus+Codex review for the later rounds. Those Codex verdicts survive only as prose claims.
Given this cycle's own documented history of a fix round shipping a *false, unverified
technical claim*, an unverifiable verdict deserves to be named.

**4.3 — Blast radius is wider than the README states.** The README warns only about the
Orca context-pack hook (UserPromptSubmit) and `startup_context.py` (SessionStart). In fact
each of the two sub-account configs registers Orca's `codex-hook.sh` on **8 events**:
`PermissionRequest`, `PostToolUse`, `PreToolUse`, `SessionStart`, `Stop`, `SubagentStart`,
`SubagentStop`, `UserPromptSubmit`. If the file goes untrusted, all of them stop — not
just same-event ones.
*Severity:* `codex-hook.sh` is a **telemetry poster** — it POSTs the hook payload to Orca's
local agent-hook HTTP endpoint (pane/tab/worktree identity) and exits 0 when the Orca env
vars are absent. So the degradation is Orca losing live Codex pane status/notifications in
those two accounts. Not a correctness or safety failure.

**4.4 — Materially good news: the Codex worker pool is unaffected.** The pool accounts
`~/.codex-profiles/acct-a`, `acct-b`, `acct-c` contain **no `hooks.json` at all**. The
installer's discovery targets `local-homes/.codex` plus `local-homes/codex-accounts/<uuid>`
only. **`codex-bulk` / `codex-design` / `codex-fast` / `codex-work` are untouched — your
cross-model dual-review capability survives the install intact.**

**4.5 — One unconditional behavior change that is not opt-in** (design §17.3): the
redaction fix changes the live hook's output for every existing install the moment it
lands. It is the safe direction (strictly more redaction, never less), but it is a real
change to a running component. Documented accepted over-redaction: hyphenated prose such
as `low-key:` gets redacted, so expect marginally more `[REDACTED]` noise in injected
context.

**4.6 — The 5 documented §19.3 limitations do not block this install.** All five sit in
non-default-parameter paths. Four are unreachable at default parameters. The one whose
code *does* execute on the default install path — §19.3 item 3 (`make_release()` builds its
own `--bridge-id` fragment independently of `_bridge_id_marker_texts()`) — is correct today
only *coincidentally*: `shlex.quote()` on the special-character-free default `BRIDGE_ID`
yields the bare form the detector expects. Nothing tests that coupling. It is not a live
bug; it is a latent trap for whoever next edits either side.

**4.7 — Credential rotation is still yours to do.** The report records that the user was
advised to rotate the exposed credential. Installing this fix stops *future* leaks; it does
nothing about the one already exposed. **If that rotation has not happened, it is more
urgent than this install.**

---

## 5. Recommended checks before you run it

**5.1 — Already done for you (no action needed).** Current state is clean and verified:
- `install_bridge.py plan` → `ok: true`, `pending_transaction: false`, all 3 configs
  `will_change: true`
- `install_bridge.py verify` → `ok: true`, `unreachable: []`, all 3 configs healthy
- full test suite on the deployment-pinned `/usr/bin/python3` 3.9.6:
  **249/249 passing** (54 + 79 + 2 + 114)

No half-finished transaction needs recovering first.

**5.2 — The one check I would actually gate on: have a post-install diagnostic ready.**

This is the real trap, and it is structural. The hook swallows every
`BridgeError`/`OSError`/`ValueError` and returns 0 with no output
(`claude_memory_hook.py:1494`). **A broken deployment looks exactly like a working one from
the outside: silence.** Hooks-untrusted *also* produces silence. After install you can
therefore have two independent silent-failure modes stacked with no way to tell them apart
— and no error anywhere.

So do not treat "no memory context appeared" as evidence of the trust state. Instead:

1. **Before install**, note the working baseline — the fixed script emits 4174 B for
   `cwd=/Volumes/Extreme SSD/Orca/projects/orca` with a memory-relevant prompt. (I verified
   this; the harness is at `…/scratchpad/smoke2/`.)
2. **After install**, re-trust hooks interactively on all 3 accounts.
3. **Then confirm delivery** by running a real `codex exec` in that cwd and checking the
   memory context actually arrives. If it does not, run `install_bridge.py verify` to
   separate "hook broken" from "hooks still untrusted" — `verify` reports config
   reachability directly and does not depend on the trust state.

**5.3 — Rollback is real, and worth knowing the shape of.** Releases are content-addressed,
so the current `bb8f8eb2…` release directory stays on disk untouched after install; the new
release lands at `1fa909cd…`. Install journals each config and takes backups, and
`recover` / `uninstall` exist and are exercised by the test suite. Reverting restores the
old *script*, but it does not un-disturb hooks trust — you would re-trust either way.

---

## 6. Bottom line, stated as two separate decisions

**Should the redaction fix be deployed? — Yes, and I would not wait long.** A verified,
reproduced secret-leaking bug is running in production right now, in exactly the text shape
this environment produces constantly. The fix is byte-identical to current behavior on 24/24
real-data runs and strictly safer on the leak shapes. The install action itself is atomic,
journaled, receipted, rollback-capable, starting from a verified-clean baseline, and — proven
empirically, not assumed — behaviorally identical to the version that produced the current
install. The cost is one interactive re-trust across 3 accounts and a temporary loss of Orca
pane telemetry in 2 of them. That is a good trade.

**Should anything write-trigger / SessionEnd related be activated? — No, and it cannot be.**
Not by this install, not by any CLI invocation that exists. `install()` takes no parameters,
the CLI has no event flag, `write_candidate_capture.py` is not in the release payload, and
the resulting `hooks.json` contains no `SessionEnd` key and no `write_candidate` string.
Wiring it remains a separate, later decision that would require new code — and, per design
§3.1, still owes the ≤10% human-reject judgment on the 130-candidate dogfood sample, which
only the user can produce.

**The single most important caveat:** the last recorded review verdict on `install_bridge.py`
is NO-GO, closed by decision rather than by re-review (§4.1). I am comfortable recommending
the install anyway *only* because I independently proved the unreviewed delta cannot change
what `install` does at default parameters (§2). If that empirical result had come out any
other way, my recommendation would be to run one more review round first.
