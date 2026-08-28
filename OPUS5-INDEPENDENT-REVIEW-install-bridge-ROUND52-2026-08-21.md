# OPUS5 Independent Review — install_bridge.py, Round 52 (2026-08-21)

Scope: follow-up fix addressing 4 P2 findings (P2-A/B/C/D) from the prior round.
Reviewer: Claude opus5 / max, read-only, independent. A parallel Codex review ran on the
same candidate with no coordination.

Interpreter: `/usr/bin/python3` = **Python 3.9.6** (`/Applications/Xcode.app/.../usr/bin/python3`),
confirmed — not Homebrew's 3.14.6.

Suite: **310 tests, exit 0, OK.**

Repo state verified untouched by this review (diffstat identical before and after:
`install_bridge.py` +1034, `tests/test_install_bridge.py` +1366, 2284 insertions / 116 deletions).
All bug-injection work was done on a scratchpad copy of the tree, never the repo.

`git status --short` in `完善orca/` shows exactly the expected set: 4 modified tracked files
(`install_bridge.py`, `claude_memory_hook.py`, and their two test files) plus 4 untracked files in
`claude-codex-memory-bridge/` (`AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md`,
`write_candidate_capture.py`, `tests/test_write_candidate_capture.py`,
`tests/test_nontrigger_corpus.py`). `prime-agent-integration/` and `round13-review-repros/`
ignored as instructed.

---

## Verdict: **NO-GO** (for declaring this candidate done)

No P0 and no P1 found. Two reproducible **P2**s, both with small, well-scoped fixes.
Under the standing dual-review gate ("any reproducible P0/P1 blocks"), nothing here is a
safety block — this is a *completeness* block. The decisive one (P2-1) is an incomplete
version of the very finding this round claims to close.

**A GO would not, by itself, authorize running `install-write-trigger`. That remains the
user's own separate, explicit decision.**

---

## What the fix genuinely got right (verified, not taken on trust)

### P2-A — genuinely fixed
Built independent fixtures with my own `System.Text.Json`-style encoder (quote/apostrophe →
`\uXXXX`, backslash → `\\`, control chars → short escapes) and an all-`\uXXXX` encoder, in both
hex cases. Matrix of **3 value shapes × 7 whitespace runs × 5 encoders = 105 combinations**,
each verified to be legal JSON that round-trips to the exact owned command:
**zero divergences** between the structural detector and the raw-bytes scanner.

- No regression on `json.dumps()`-style recognition (bare / double-quoted / single-quoted all True).
- Negative controls stay negative (foreign bridge id; `--other-flag <id>` with no `--bridge-id`).
- Non-BMP input is safe: `_json_string_body_u_escaped` passes it through unchanged (no malformed
  5-hex-digit escape, no crash), and `_json_string_body`'s `ensure_ascii=True` already emits the
  surrogate-pair form — the two functions together cover both `ensure_ascii` conventions.
- Value forms: 6, patterns: 30 — matches my own independent derivation.
- Revert-verified myself: dropping the `\u` forms fails **9/9** sub-variants.

### P2-B — the rewrite is real
By my own arithmetic, `marker_start = padding_len + marker_offset_in_raw` collapses to exactly
`chunk_size - 5`, and `marker_end = chunk_size + 35` (marker is 40 bytes) — a genuine straddle,
not the claimed-but-false straddle of the original. Injecting `overlap_len = 2` produces
**2 failures**, as claimed. (See P3-3 for the residual.)

### P2-C — validation genuinely precedes all I/O
Statement-order traced in the live function: `_validate_bridge_id` at **:1578**, first I/O
(`resolve_ssd_path(SOURCE_SCRIPT)`) at **:1583**, first consumption of the value
(`_bridge_id_command_fragment` → `shlex.quote`) far below at **:1712**. All claimed failure modes
now raise clean `InstallError`: `None`, non-str `123`, `''`, `{}` (empty dict), `['x']`, `True`,
`b'x'`. Revert-verified: **3/3** new tests fail without the fix (1 FAIL = the silent empty-id
corruption, 2 ERROR = the bare `TypeError`/`KeyError`).

### P2-D — genuinely reachable from real code, not just the test
`_untracked_owned_including_write_trigger()` has exactly two call sites, both real:
`recover_pending_install()` **:2059** and `uninstall()` **:2632**. Behavior confirmed by
instrumenting `_find_untracked_owned_configs`:

- base-only receipt → **exactly 1** scan `(BRIDGE_ID, "UserPromptSubmit")` — unchanged.
- write-trigger receipt → **2** scans, adding `(MODULE_ID, "SessionEnd")`.

The receipt field is real (`:1711` → `:2412`) and `_validate_receipt_shape` (`:1811-1819`)
enforces it is a non-empty str paired with the script sha; a garbage value is additionally
caught by `_find_untracked_owned_configs`'s own `_validate_bridge_id` (`:1084`). The new test is
honestly constructed — line 3112 asserts the base-identity scan alone returns `[]`, proving the
fix is load-bearing. Revert-verified: **1/1**.

### Core invariants — CONFIRMED
- **MEMORY.md never written.** `claude_memory_hook.py` contains zero write syscalls (AST sweep);
  its MEMORY.md read path is `os.O_RDONLY | O_NOFOLLOW` with fstat dev/ino re-verification
  (`:439-443`). Every `atomic_write` destination is either statically pinned under `RUNTIME_BASE`
  or a literal `hooks.json` from `_enumerate_hook_configs()`.
- **No auto-promotion.** Both SessionEnd registration sites (`:2282`, `:2736`) sit inside
  `if write_trigger is not None:`; the only producer of a non-None value is
  `install_write_trigger()`, and `main()` calls `install()`/`plan()` with no arguments.
  `recover_pending_install()` has no branch that writes the installed "after" bytes — it can only
  ever revert. `claude_memory_hook.py` never reads the write-candidates tree, so a captured
  candidate can never reach what is published to Codex.
- **`install-write-trigger` is a separate explicit action**, one call site, hard-requires
  `--write-trigger-policy`, not reachable from any other action.

### Default UserPromptSubmit path — byte-identical vs `HEAD`
Verified empirically by loading committed-HEAD and working-tree `install_bridge.py` side by side
as two modules and diffing their outputs, not by reading the diff:

- `make_handler(cmd)` — identical (`timeout: 5`, unchanged `statusMessage`).
- `update_hook_config(raw, cmd)` — identical across 4 scenarios (empty file, existing unrelated
  handler, already-installed/idempotency, other-event-present).
- `make_release()` with no `write_trigger` — **all 11 result keys identical**, including
  `release_id`, `policy_raw`, `policy_sha256` and the full `command` string.

### Holistic re-check (P2-A × P2-C × P2-D)
- P2-A's wider form set produces **no new false positives** for P2-D's second scan: 4 benign
  fixtures (a comment mentioning `--bridge-id`, a `--bridge-id-suffix` near-miss, a foreign id, a
  log line containing MODULE_ID) all stay False under both identities; only a fixture genuinely
  carrying the marker returns True.
- Cost is fine: 30 patterns, 2 MB scanned in ~3 ms per identity.
- The whitespace alternation is prefix-free, so the `{1,8}` bounded repetition cannot backtrack
  catastrophically.
- `overlap_len` is a true upper bound (global max over all patterns, exactly
  `len(prefix) + 8·widest_ws + len(suffix)`), and I confirmed a real 395-byte straddle is found.

---

## Findings

### P2-1 (NEW) — `make_release()` validates 1 of the 3 keys it consumes from the same caller-supplied dict; the P2-C fix is incomplete

`install_bridge.py:1578` validates only `bridge_id`. The same function then consumes two more
keys from the same arbitrary dict with unguarded subscripts:

- `install_bridge.py:1636` — `write_trigger["max_candidates_per_project"]`
- `install_bridge.py:1637` — `write_trigger["max_candidate_bytes"]`
- `install_bridge.py:1613-1614` — the whole dict goes into `canonical_json(release_key_fields)`

Reproduced against the **current** code:

| input | result |
|---|---|
| `{"bridge_id":"ok-id","max_candidate_bytes":10}` | bare `KeyError: 'max_candidates_per_project'` |
| `{"bridge_id":"ok-id","max_candidates_per_project":5}` | bare `KeyError: 'max_candidate_bytes'` |
| limit = `object()` | bare `TypeError: Object of type object is not JSON serializable` |
| limit = `{1,2}` | bare `TypeError: Object of type set is not JSON serializable` |

These are the exact defect class P2-C was raised for: a bare, uncaught non-`InstallError`
escaping `main()`'s `except InstallError`. The fix's own comment at **:1576-1577** states the
rationale — *"`.get()` rather than `["bridge_id"]` so a write_trigger dict missing the key
entirely also fails this same clean way instead of a bare KeyError"* — and applies it to exactly
one of the three keys.

**The worse, silent variant.** A JSON-serializable but wrong-typed or out-of-range limit passes
`make_release()` **cleanly** and is baked into the installed `policy.json`:

`"5"`, `5.0`, `True`, `None`, `[5]`, `-1`, `0`, `10**30` — all accepted by `make_release()`, all
then rejected at runtime by `write_candidate_capture._parse_write_trigger_block()`
(*"must be an integer"* / *"out of range"*), and that rejection is swallowed by `scan()`'s
`except Exception` backstop → exit 0, no output, no diagnostic, **forever**. That is a
permanently undetectable broken install — the same harm shape as `bridge_id=None`'s empty-id
corruption that P2-C itself cites, and the same shape as the `--write-candidates-root` P0 already
fixed at `:1689`. `verify()` would not catch it either: the script and policy hashes both match.

Reachability is exactly the premise the fix itself states at **:1549-1553** ("reachable directly
by any library caller of this module ... with an arbitrary dict"). Not reachable via the CLI,
where `_load_write_trigger_config()` always produces validated ints.

**Fix:** validate all three keys at `:1578` (e.g. `isinstance(v, int) and not isinstance(v, bool)`
plus a range check for the two limits), not just `bridge_id`.

### P2-2 (NEW) — an ordinary `install` silently disarms the P2-D safety net

The P2-D scan is conditional on `receipt.get("write_trigger_bridge_id") is not None`
(`install_bridge.py:1389-1390`). A plain `install()` destroys that condition while leaving the
handler live:

- `install_bridge.py:2409-2416` — `write_trigger_receipt_fields = {}` when `write_trigger is None`,
  so the new receipt drops `write_trigger_bridge_id` / `write_trigger_script_sha256`.
- `update_hook_config` (`:1489`) only rewrites the one event it is given, so a pre-existing
  SessionEnd write-trigger handler survives untouched.

Reproduced end to end by replicating the P2-D fix's own test fixture and inserting one plain
`install()`:

```
[A] receipt has write_trigger_bridge_id : True
    base-identity scan finds            : []          <- P2-D is load-bearing here
    combined P2-D scan finds            : the stray   <- and it works
[B] after plain install():
    receipt has write_trigger_bridge_id : False
    combined P2-D scan now finds        : []          <- net disarmed
    managed hooks.json SessionEnd count : 1           <- handler still live
[C] uninstall(): SUCCEEDED ok=True
    receipt deleted                     : True
    STRAY write-trigger handler alive   : 1           <- abandoned
```

Control (no plain `install`): `uninstall()` correctly **REFUSED**.

That final state — receipt deleted while an owned handler stays live at an untracked path — is
precisely the harm P2-D exists to prevent, and the same shape as R8-P1-B / R11-P1-B for the base
identity. Secondary effect: after the plain `install`, `verify()` (`:2525`) stops checking the
SessionEnd handler entirely and still reports `ok: true`.

`install` is a realistic action for a user with the write-trigger installed (it is *the* action
for picking up an updated `claude_memory_hook.py`). Independently reproduced by a second
read-only reviewer agent working from a different angle.

**Fix:** carry `write_trigger_*` forward from the previous receipt when `write_trigger is None`,
or refuse a plain `install` when the previous receipt carries them. Whichever is chosen, the
conditional nature of the P2-D guard should be stated in
`_untracked_owned_including_write_trigger()`'s comment, which currently reads as unconditional.

### P3-1 — ` ` is not covered for the space separator

`_BRIDGE_ID_U_ESCAPABLE_CHARS` (`:698`) omits `" "`, so the whitespace alternation contains
`	`, `
`, `` (both cases) but **not** ` ` — asymmetric treatment of the one
separator that is by far the most common. Demonstrated: `{"c":"x --bridge-id <BRIDGE_ID> y"}`
is legal JSON, decodes to exactly the owned command, and
`_raw_bytes_contain_bridge_marker` returns **False**.

No mainstream encoder escapes a plain space (json.dumps, System.Text.Json, Jackson, Go
encoding/json all leave it literal), so this is not realistically reachable — but the constant's
stated rationale (*"an ordinary encoder never `\uXXXX`-escapes plain ASCII letters/digits"*) does
not actually address space, which is neither a letter nor a digit. Either add ` ` or amend
the comment to say space was consciously excluded and why.

### P3-2 — mixed-rendering encoders are not modeled

The fix renders each value shape either all-shorthand or all-`\u`, never mixed. A real
System.Text.Json document *is* mixed (quote → `"`, backslash → `\\`, tab → `\t`).
Not reachable today: a miss requires ≥2 different escapable classes in one shape, and both real
identities (`orca-claude-native-memory-v1`, `orca-claude-codex-memory-write-trigger-v1`) are
`[a-z0-9-]` only — verified, 0 missed forms for both. It is not *structurally* prevented, though:
`_validate_bridge_id` (`:445`) accepts any non-empty string, and a bridge_id containing a
backslash or tab does produce misses (e.g. `back\slash-id` → `"back\\slash-id"` is
unmatched). Worth one line in the comment noting the assumption.

### P3-3 — P2-B's rewritten test pins only a 5-byte straddle

The rewrite fixed the "doesn't straddle at all" defect, but leaves the *size* of the overlap
window untested. I capped `overlap_len` at 5, 38, 100, 200 and 394 — **all 310 tests still pass
in every case**, though the code's real bound is 395 and I demonstrated a legitimate marker
(utf-32, 8× `	` run, `"`-quoted value = 396 bytes) that genuinely requires 395.
A future regression to a plausible-but-too-small constant would ship green. Suggest a second
subtest that straddles with `max_len - 1` bytes in the first chunk.

### P3-4 — stale module docstring (file not in this diff)

`write_candidate_capture.py:5-10` still asserts *"This module is standalone and NOT wired into
any live hook: nothing in the existing, reviewed claude_memory_hook.py / install_bridge.py
imports or calls it, and it registers no SessionEnd handler itself."* Both clauses are now false:
`install_bridge.py:2793` imports it and `:2282` registers a real SessionEnd handler running it.
The hard-invariant paragraph immediately below (`:12-19`) is still accurate.

### P3-5 — receipt-driven writes carry no `hooks.json` basename constraint

`_receipt_rows` builds write targets from `raw_row["path"]` via `resolve_ssd_path`, which enforces
only SSD containment — there is no basename check anywhere. Because `~/.claude` resolves onto the
SSD, MEMORY.md files do pass that containment test. Not exploitable: the write is gated behind
`after_mode == 0o600` in both `_validate_receipt_shape` (`:1868`) and `_receipt_rows` (`:1972`),
and real MEMORY.md files are 0644; anyone who could forge the 0600 receipt inside the 0700
runtime dir already has same-uid write access to MEMORY.md directly. Logged as structural
hardening only: an explicit `path.name == "hooks.json"` assertion would make the invariant
structural rather than emergent from a mode coincidence.

---

## Summary

| Claim | Status |
|---|---|
| P2-A `\uXXXX` recognition, 9 revert-verified sub-tests | **Confirmed** (105-combo independent matrix, 0 divergences; 9/9 revert) |
| P2-B chunk-boundary test genuinely straddles | **Confirmed** (own arithmetic; `overlap_len=2` → 2 failures). Residual: P3-3 |
| P2-C validation before any I/O, 3 revert-verified tests | **Confirmed for `bridge_id`**; **incomplete** — see P2-1 |
| P2-D behavioral gap closed in real call paths | **Confirmed** as written; **conditional** — see P2-2 |
| 310 tests passing | **Confirmed**, exit 0, Python 3.9.6 |
| MEMORY.md never written / no auto-promotion | **Confirmed** |
| Default UserPromptSubmit path byte-identical | **Confirmed** empirically vs HEAD |

Two P2s, no P0/P1. Both fixes are small and localized. Recommend one more round.
