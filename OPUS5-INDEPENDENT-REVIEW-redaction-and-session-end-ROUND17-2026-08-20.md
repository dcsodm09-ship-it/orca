# OPUS5 Independent Review — Round 17

**Scope:** the follow-up fix in `claude-codex-memory-bridge/install_bridge.py` closing the two P2s
from the round-16 whole-candidate acceptance review (`bridge_id` parameterization + reachable
`timeout=None`), plus its test file and design-doc §17.3.
**Interpreter:** `/usr/bin/python3` = Python **3.9.6** (`Clang 21.0.0 (clang-2100.1.1.101)`), confirmed.
**Date:** 2026-08-20
**Mode:** read-only. No repo file modified. All verification against isolated copies in the session scratchpad.
**Independence:** a parallel Codex review ran on the same candidate; no coordination, no shared findings.

---

## 0. Verdicts

| Item | Verdict |
| --- | --- |
| Fix 1 — `bridge_id` threading | **GO**, with one new P2 (incomplete chain) |
| Fix 2 — reachable `timeout=None` | **GO**, clean |
| Fix 3 — design doc §17.3 | **GO**, accurate |
| **This fix overall** | **GO** — no P0, no P1 |
| Whole 3-file candidate | **Conditional GO** — see §6 |

Three new P2s found, all latent (unreachable from any call site that exists today), all in the same
"only bites once the SessionEnd capability is actually wired" class as the findings being closed.

---

## 1. Claims verified TRUE

**Fix 1.** All five named functions take `bridge_id: str = BRIDGE_ID` and thread it end to end —
verified by runtime signature introspection and by reading every call site, not by trusting the report:

- `owned_handler()` `install_bridge.py:389`, comparison at `:431`
- `_owned_shape_match()` `:546`, forwards at `:564`
- `_raw_bytes_contain_bridge_marker()` `:567`, marker built at `:580`
- `_attempt_structural_detection()` `:587-588`, forwards at `:622` and `:627`
- `_contains_owned_handler()` `:633-639`, forwards at `:711-713` and `:718`
- `update_hook_config()` gained it at `:1040`, uses it at `:1081` — the fix is genuinely reachable
  through the real config-writing API.

**Fix 2.** `make_handler()` `:1015` is `timeout: int | None = DEFAULT_HOOK_TIMEOUT`; the key is
omitted entirely when `None` (`:1030-1031`). `update_hook_config()` forwards at `:1083`.
`make_handler()` is the **only** handler-dict construction site in the file — grep for `"timeout"`
across `install_bridge.py` returns only `:1031` plus comments; `:151`'s `timeout=3` is a
`subprocess.run` timeout, unrelated; `write_candidate_capture.py:2330-2336` is a commented-out
example block that correctly carries no timeout key. **No other path hardcodes `"timeout"`.**

**Fix 3.** Design doc scope note at `AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md:408` and §17.3 at `:746`
correctly separate the opt-in SessionEnd/`bridge_id`/`timeout` capability from §14–§16's
unconditional, already-live `claude_memory_hook.py` redaction change. Accurate as written.

**Suite.** `Ran 246 tests ... OK`, exit 0, on `/usr/bin/python3` 3.9.6. 246 confirmed by count.

---

## 2. Zero-regression claim — verified independently, and it holds

Not taken from the report. I extracted the pre-diff `HEAD` version of `install_bridge.py` and
imported it alongside the working-tree version as two live modules, then differentially compared
every default-path call across a corpus of 17 hooks.json documents and 12 raw byte blobs
(UTF-8/16/32/latin-1, truncated, non-JSON, duplicate-key, marker-past-size-bound, 100 KB padded),
× 3 structural size bounds × 2 command strings × `remove` on/off — roughly 2000 comparisons of
`owned_handler`, `_owned_shape_match`, `_raw_bytes_contain_bridge_marker`,
`_attempt_structural_detection`, `_contains_owned_handler`, `make_handler` (including canonical-JSON
byte equality) and `update_hook_config`.

**Result: zero behavioral differences.** The only 4 non-identical results are one error-message
string change, below. The default `(BRIDGE_ID, DEFAULT_HOOK_EVENT, DEFAULT_HOOK_TIMEOUT)`
UserPromptSubmit path is confirmed unchanged — this is a stronger check than the report's own.

---

## 3. New findings

### P2-R17-A — `_stream_scan_oversized_for_bridge_marker()` is a sixth detection-chain member, and it was missed

`install_bridge.py:726`, marker built at `:748`:

```python
marker_variants = [
    f"--bridge-id {BRIDGE_ID}".encode(encoding)
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
]
```

It takes **no** `bridge_id` parameter. It is the bounded, constant-memory twin of Stage 2's
`_raw_bytes_contain_bridge_marker()` — same marker, same five encodings, same job — for files that
exceed `MAX_MANAGED_FILE_BYTES`. The module header comment at `:43-45` enumerates the chain as
"`owned_handler()`, `_owned_shape_match()`, `_attempt_structural_detection()`,
`_contains_owned_handler()`, and Stage 2's `_raw_bytes_contain_bridge_marker()`" — this function is
absent from that list, so the comment's own enumeration of the chain is incomplete.

Its only caller, `_find_untracked_owned_configs()` `:778`, is likewise unparameterized and calls
**both** halves of the chain — the parameterized `_contains_owned_handler()` at `:1007` and the
unparameterized `_stream_scan_oversized_for_bridge_marker()` at `:967`.

Reproduced: an oversized (4194492 B > `MAX_MANAGED_FILE_BYTES` 4194304) relocated `hooks.json`
carrying a genuine `--bridge-id orca-claude-codex-memory-write-trigger-v1` SessionEnd handler →
scan returns `False`. Byte-identical file carrying this module's own `BRIDGE_ID` → `True`.

**Failure scenario.** The moment someone threads `bridge_id` into `_find_untracked_owned_configs()`
— the obvious next step when the SessionEnd capability is wired — the untracked-owned safety scan
becomes correct for configs ≤ 4 MB and silently blind for configs > 4 MB. `uninstall()` /
`recover_pending_install()` would then delete the receipt while an oversized relocated config still
holds a live handler. That is exactly R12-P1-A / R13's abandoned-live-handler bug, re-opened for
the non-default `bridge_id`. Latent today (no call site passes a non-default id) — the same
reachability class as the P2 this round set out to close, which is why it should be closed with it.

### P2-R17-B — encoding-dependent inconsistency for a non-default event whose key is absent

`_attempt_structural_detection()` `:619-629` handles a `None` from `_owned_shape_match()` two
different ways:

- strict path `:622`: `return bool(_owned_shape_match(...))` → coerces `None` to `False`, definitive
- lenient path `:626-628`: `if match is not None: return match` → treats `None` as "keep looking",
  falls through to Stage 2's **event-blind** marker search at `:718`

With the default event, `None` only ever meant "not our document shape at all", so the two agreed.
With a non-default `event`, `None` now *also* means "our document, but that event key is absent" —
and the paths disagree.

Reproduced. Identical logical document `{"hooks": {"UserPromptSubmit": [<own handler>]}}`,
`_contains_owned_handler(raw, event="SessionEnd")`:

| encoding | result |
| --- | --- |
| utf-8 | `False` |
| utf-16 | `InstallError: cannot rule out an owned hook handler...` |
| utf-16-le | `InstallError` |
| utf-32 | `InstallError` |

Fail-closed direction, so an availability defect, not a safety hole: a well-formed config that
simply has no SessionEnd key yet hard-raises instead of answering "not registered". Latent —
`_contains_owned_handler()` is only ever called with defaults today.

### P2-R17-C — no validation on `bridge_id`, `event`, or `timeout`

These parameters exist precisely so an *external* module supplies the values, but nothing rejects
degenerate ones. All reproduced:

- **`bridge_id=""`** — `owned_handler()` correctly returns `False`, but
  `_raw_bytes_contain_bridge_marker(..., bridge_id="")` searches for the literal `"--bridge-id "`
  and so matches **any** third-party command containing that flag. `_contains_owned_handler()` on
  unparseable foreign bytes then raises `cannot rule out an owned hook handler`. Stage 0/1 and
  Stage 2 disagree.
- **`bridge_id="a b"`** (whitespace) — `owned_handler()` `True` (shlex sees the quoted token),
  `_raw_bytes_contain_bridge_marker()` `False` (literal substring misses the quotes). This
  divergence is **fail-open**, the opposite direction from the one above.
- **`event=""`** → writes `hooks[""]`. **`event="hooks"`** → writes `hooks["hooks"]`. Both accepted.
- **`timeout=0`** → writes `"timeout": 0`. Legal for `int | None`, but a hook runner reads 0 as
  "time out immediately" — semantically the *opposite* of `None`'s "no bound", i.e. the single
  value most likely to be passed by mistake in place of `None` and the one that most inverts intent.
  **`timeout=True` / `False`** → writes JSON `true` / `false` (Python `bool` is an `int`).

None reachable from this file's own call sites. A cheap guard in `update_hook_config()` (non-empty
`bridge_id` with no whitespace; non-empty `event`; `timeout is None or (type(timeout) is int and
timeout > 0)`) closes all of them.

### P3 — error-message text change (behaviorally zero)

`{"hooks": {"UserPromptSubmit": "notalist"}}`: old `InstallError("missing UserPromptSubmit hook
list")` → new `InstallError("malformed UserPromptSubmit hook list")` (`:1054`). Same exception type,
same fail-closed outcome; these were the only 4 of ~2000 differential comparisons that differed.
The genuinely-missing-key case still raises `missing ... hook list` (`:1073`), correctly preserving
the round-16 fail-closed pin. Nothing in the tree greps the old string; design doc `:424` documents
the new text. No action needed — recorded for completeness.

---

## 4. Break attempts that FAILED (fix holds)

- Non-default `bridge_id` through `update_hook_config()`: idempotent (1 handler, not 3), `remove=True`
  clears it, both detectors report `True`. The round-16 repro no longer reproduces.
- Default-path handler bytes identical to pre-diff `HEAD` under canonical JSON.
- A handler carrying a *different* `bridge_id` stays invisible to every default call — no
  cross-ownership leakage in either direction.
- Missing `UserPromptSubmit` key still fails closed; present-but-malformed still fails closed.
- No second handler-construction site hardcoding `"timeout"` anywhere in the candidate.

---

## 5. Scope check — `git status --short` (claim partially inaccurate)

`round13-review-repros/` — untracked, mtime **Aug 19**, untouched this round. Confirmed clean.

`prime-agent-integration/` — **not** untouched. The working tree carries uncommitted changes to
`install_prime_agent.py` and `tests/test_install_prime_agent.py`: **+1160 / −58 lines**, with mtimes
`17:38:55` and `17:43:27` — interleaved within seconds of this round's `install_bridge.py`
(`17:38:05`) and `test_install_bridge.py` (`17:43:08`), i.e. a concurrent agent was editing it during
this review.

It is genuinely a **separate workstream** — the diff has **zero** occurrences of `bridge_id`,
`SessionEnd`, `make_handler`, `DEFAULT_HOOK`, or `write_candidate`, and the git log shows
prime-agent rounds 40–43 in flight. So there is no content overlap and no correctness impact on
this candidate.

But the accurate statement is *"no overlap with this round's changes"*, **not** *"nothing in
prime-agent-integration touched"*. **Scope hazard:** a `git commit -a`, or handing over "the working
tree" as the candidate, would sweep in 1160 lines of unrelated, separately-under-review prime-agent
work. Any commit for this candidate must be path-scoped.

---

## 6. Final verdict

**This fix: GO.** No P0, no P1. Both round-16 P2s are genuinely closed and genuinely reachable
through the real API, and the zero-regression claim survives my own independent differential
verification. The three new P2s are latent follow-ups, not blockers.

**Whole 3-file candidate (`write_candidate_capture.py` + `install_bridge.py` +
`claude_memory_hook.py`): CONDITIONAL GO** — ready to hand to a human as a reviewed,
deployable-pending-authorization candidate, subject to three conditions:

1. **P2-R17-A must be closed before the SessionEnd capability is actually wired in.** Shipping
   `install_bridge.py` as-is is safe (nothing calls it with a non-default `bridge_id`), but wiring
   the write-trigger without parameterizing `_stream_scan_oversized_for_bridge_marker()` and
   `_find_untracked_owned_configs()` re-opens R12-P1-A for oversized configs. Either fix it now or
   track it as an explicit, named precondition on the wiring step.
2. **P2-R17-B and P2-R17-C** either fixed or explicitly accepted in writing as known-latent.
3. **The prime-agent scope hazard in §5 resolved** — path-scoped commit only.

Scope caveat on my own attestation: this round I re-reviewed `install_bridge.py`, its tests, and the
design doc. `claude_memory_hook.py` and `write_candidate_capture.py` were unchanged since the
round-16 review (mtimes `16:40:19` / `16:40:33`, both predating this round's edits) and I re-ran but
did not re-audit them here; my GO for those two rests on round 16, not on fresh work.

**Reminder:** a GO here authorizes nothing. Per standing rule, actually installing or activating this
bridge still requires separate, explicit user authorization.
