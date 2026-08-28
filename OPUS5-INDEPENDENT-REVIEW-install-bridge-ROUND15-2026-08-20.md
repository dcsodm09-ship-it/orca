# Independent Claude opus5/max read-only review — install_bridge.py, round 15

Date: 2026-08-20 · Reviewer: Claude opus5 (max effort), independent · Interpreter: `/usr/bin/python3` 3.9.6
Scope: `完善orca/claude-codex-memory-bridge/install_bridge.py` + `tests/test_install_bridge.py`
Method: full diff read, own repro harness (not the candidate's tests), full suite re-run.
A parallel independent Codex review ran on the same candidate; no coordination.

---

## Verdict

**NO-GO for this fix.** One P1 remains open: the `bridge_id` parameterization is still incomplete,
now at `_find_untracked_owned_configs()` — which accepts `bridge_id` but not `event`, and drops
`event` when calling the one detector that needs it. This is the *same defect class, in the same
chain*, as the finding this round was convened to close, and this round's own header comment makes
an explicit false promise about it.

All four claimed changes are otherwise real, correctly implemented, and regression-free.

---

## Confirmed claims

| Claim | Status | Evidence |
|---|---|---|
| 1. `_stream_scan_oversized_for_bridge_marker(bridge_id=)` + caller forwards | ✅ | `:756`, `:817`, forwarded `:1015` and `:1055` |
| 1b. Header enumerates all 6 `bridge_id` chain functions | ✅ | `:46-48` — 6 named, matches signature introspection |
| 2. `_bridge_id_marker_texts()` shared by both raw scanners | ✅ | defined `:576`, used `:610` and `:787` |
| 3. `update_hook_config()` rejects bad `bridge_id`/`event` | ✅ | `:1110-1119` |
| 4. `timeout=0` vs `None` documented | ✅ | `:1074-1080` |
| 6. 248 tests pass | ✅ | `Ran 248 tests ... OK` (54 hook + 78 install + 2 corpus + 114 wcc) |

**Q2 — my own prior-round repro re-run against current code.** Oversized (>4 MiB) hooks.json under
RUNTIME_BASE with a `SessionEnd` handler under `bridge_id=orca-claude-codex-memory-write-trigger-v1`:

```
_find_untracked_owned_configs(set(), bridge_id=OTHER)
  -> InstallError: cannot rule out an owned hook handler: oversized content under
     RUNTIME_BASE matches the bridge marker (at .../backups/relocated-acct/hooks.json)
_find_untracked_owned_configs(set())                      -> []   (default unchanged)
_find_untracked_owned_configs(set(), bridge_id="other")   -> []   (no false positive)
```
Genuinely fixed. Not taken on trust from the new test.

**Overlap math.** `overlap_len = max(len(v) for v in marker_variants) - 1` over all 15 encoded
variants (3 texts × 5 encodings) = 167 B for the default id (utf-32 of the single-quoted form, no
BOM since `-le`/`-be` are explicit). ≥ every individual variant's length−1, so every variant split
across a 1 MiB chunk boundary is still found. Correct.

**Q4 — default call values pass the new guards.** `re.fullmatch(r"[A-Za-z][A-Za-z0-9]*",
"UserPromptSubmit")` matches; `BRIDGE_ID` is a non-empty `str`. Rejections verified for
`bridge_id=None`/`""` and `event=None`/`""`/`"Session End"`/`"Session_End"`; `event="SessionEnd"`
accepted; missing `UserPromptSubmit` still fails closed (`missing UserPromptSubmit hook list`).

**Q5 — zero default-path regression.** Full unmocked `plan()`→`install()`→`verify()`→`uninstall()`
against an isolated fake SSD. Installed handler dict is exactly
`{"type","command","timeout":5,"statusMessage"}` — key-for-key identical to pre-parameterization.
Pre-existing `/usr/bin/true` handler preserved; uninstall removes only ours.
`update_hook_config` idempotent, and explicit-defaults output is byte-identical to implicit.

**Q7 — scope.** `git status --short` in `完善orca/` shows 4 modified files. mtimes separate the
rounds cleanly: `install_bridge.py` 18:05, `tests/test_install_bridge.py` 18:07,
`AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md` 18:08 — this round. `claude_memory_hook.py` 16:40 /
`test_claude_memory_hook.py` 16:20 / `write_candidate_capture.py` 16:40 — earlier rounds, same
session. `prime-agent-integration/` is tracked and **clean** (`git status --short` on that path is
empty), 5 files, zero import or reference relationship with the bridge.
`round13-review-repros/` is untracked scratch from the closed round-13 cycle, all mtimes
Aug 19 16:12, nothing under `claude-codex-memory-bridge/tests/` references it. No overlap either way.

---

## P1-A — `_find_untracked_owned_configs()` takes `bridge_id` but not `event`

**`install_bridge.py:817` (signature) and `:1055` (the dropping call site).**

```python
def _find_untracked_owned_configs(receipt_paths: set[str], *, bridge_id: str = BRIDGE_ID) -> list[str]:
...
    owned = _contains_owned_handler(candidate_raw, structural_max_bytes=..., bridge_id=bridge_id)
```

`_contains_owned_handler()` takes `event: str = DEFAULT_HOOK_EVENT`. It is never passed one here, so
the untracked-owned-handler safety scan can only ever look under `"UserPromptSubmit"`.

Signature matrix — `_find_untracked_owned_configs` is the **only** function that consumes an
event-sensitive detector yet neither accepts nor forwards `event`:

```
_owned_shape_match                        event=YES  bridge_id=YES
_attempt_structural_detection             event=YES  bridge_id=YES
_contains_owned_handler                   event=YES  bridge_id=YES
update_hook_config                        event=YES  bridge_id=YES
_find_untracked_owned_configs             event=--   bridge_id=YES   <-- drops it
owned_handler / _raw_bytes_contain_bridge_marker /
_stream_scan_oversized_for_bridge_marker / _bridge_id_marker_texts
                                          event=--   bridge_id=YES   (correct: below event level)
```

### Failure scenario (reproduced)

A future caller wires write_candidate_capture's `SessionEnd` handler — the exact scenario this
whole parameterization exists for — then a relocated/untracked account config carries it.
`_find_untracked_owned_configs(set(), bridge_id=MODULE_ID)` **silently returns `[]`** in both
realistic shapes:

- **Both keys present** (the common shape — the bridge itself installs a `UserPromptSubmit` handler
  into the same file): `_owned_shape_match` finds `hooks["UserPromptSubmit"]` is a list, returns
  `False` over unrelated handlers → definitive negative → Stage 2 marker check never runs.
- **`SessionEnd` only, no `UserPromptSubmit` key**: `_owned_shape_match` returns `None`, but
  `_attempt_structural_detection:652` collapses it — `return bool(_owned_shape_match(...))` →
  `bool(None)` → `False` → definitive negative → Stage 2 never runs. (The `bool()` collapse is
  documented-intentional at `:630-633` for Layer 0 and is *not itself* a bug — it is simply what
  converts "ambiguous" into a silent miss once the wrong event is being asked about.)

Verified directly: `_contains_owned_handler(raw, event="SessionEnd", bridge_id=OTHER)` → `True`,
but the scan that wraps it → `[]`, for the identical bytes (447 B, far under the size cap).

### Why this is the same defect, inverted

The **oversized** path detects it (the stream scanner is a pure marker scan, event-agnostic); the
**normal-size** path does not. That is the exact inverse of the inconsistency this round set out to
fix, reintroduced one level up the same chain — and it is the far more likely size class.

The code makes the opposite promise explicitly, at `:823-825`:

> a future caller wiring in write_candidate_capture.py's SessionEnd handler passes
> `bridge_id=write_candidate_capture.MODULE_ID` here the same way it would to `update_hook_config()`.

Doing exactly that yields a silently blind safety scan.

### Fix shape

Add `event: str = DEFAULT_HOOK_EVENT` to `_find_untracked_owned_configs()` and forward it at `:1055`.
The stream scanner needs no change — event-agnostic there is conservative and correct.

### Severity note for the human

Zero live impact today: both call sites (`recover_pending_install():1579`, `uninstall():2091`) pass
defaults. It is latent-until-wiring — **exactly as latent as the missing `bridge_id` on
`_stream_scan_oversized_for_bridge_marker()` that this round rated P1 and fixed.** I am rating it P1
for consistency with that precedent. If the project prefers to rate by live impact, then this is P2
*and so was the previous finding*; what should not happen is the two being rated differently.

---

## P2-A — the double-quoted marker variant is dead against JSON content

**`install_bridge.py:591`.** In a hooks.json a `"` inside the command string is JSON-escaped, so the
file's raw bytes contain `--bridge-id \"<id>\"`, never `--bridge-id "<id>"`:

```
double-quoted   raw bytes around marker = b'--bridge-id \\"orca-claude-code...'
  _raw_bytes_contain_bridge_marker(raw, bridge_id=OTHER) -> False
  the JSON-escaped form --bridge-id \"<id>\" IS present in those bytes -> True
```

Single-quoted works (no JSON escaping needed) — so the comment at `:580-588` closes half of what it
claims. Matters most for `_stream_scan_oversized_for_bridge_marker()`, the one path where a
perfectly valid >4 MiB JSON file never gets a structural read at all.

## P2-B — whitespace variance is the same unclosed class, unmentioned

`--bridge-id  <id>` (two spaces) and `--bridge-id\t<id>` both `shlex.split()` to the owned argv pair,
so `owned_handler()` calls them owned — but neither raw scanner matches:

```
bare            shlex-owned=True  stream=True  rawbytes=True   AGREE
single-quoted   shlex-owned=True  stream=True  rawbytes=True   AGREE
double-quoted   shlex-owned=True  stream=False rawbytes=False  MISMATCH
double-space    shlex-owned=True  stream=False rawbytes=False  MISMATCH
tab-separated   shlex-owned=True  stream=False rawbytes=False  MISMATCH
```

The claim that returning all forms "from one place keeps both fallback scanners in sync going
forward" overstates what a fixed text list can guarantee: it is a lossy approximation of
`owned_handler()`'s shlex semantics, and nothing tests the coupling.

## P2-C — answer to Q3: yes, there is a 7th independent marker construction

**`make_release()` at `install_bridge.py:1249-1261`** builds `["--bridge-id", BRIDGE_ID]` and joins
with `shlex.quote`. It is the *producer*, so it cannot "miss" — but `_bridge_id_marker_texts()` is
correct only because `shlex.quote` happens to emit the bare or single-quoted form. Nothing enforces
or tests that coupling. Concrete drift:

- switching to `--bridge-id=<id>` style, or to `subprocess.list2cmdline` (emits double quotes),
  silently breaks both raw scanners with no test failing;
- a bridge_id containing a single quote — `shlex.quote("id'quote")` → `'id'"'"'quote'` — matches
  none of the three marker texts, and `update_hook_config()` deliberately declines to constrain
  bridge_id's format (`:1105-1109`), so that input is explicitly permitted.

(write_candidate_capture.py's own sketched wiring at `:2329` also uses
`" ".join(shlex.quote(part) ...)`, which is what bounds the practical impact today.)

## P2-D — the "one public API boundary" claim is inaccurate

`owned_handler()` is public (no underscore) and takes `bridge_id` unvalidated;
`_find_untracked_owned_configs()` is the safety-scan entry point and also takes it unvalidated.
Verified: `owned_handler(..., bridge_id=None)` returns `False` silently, and
`_bridge_id_marker_texts("")` returns `('--bridge-id ', '--bridge-id ""', "--bridge-id ''")` — an
empty bridge_id makes the raw scanners match *any* bridge command. Not reachable via
`update_hook_config()`, but reachable via the very entry point a future SessionEnd caller is
directed to use.

## P2-E — header comment names a function that has no `event` parameter

**`install_bridge.py:30-32`** lists `make_handler()` among functions that "take an optional `event`
parameter." `make_handler(command, *, timeout)` has none. Confirmed by signature introspection.
(The parallel 6-function `bridge_id` enumeration at `:46-48` is correct.)

---

## Cross-file references spot-checked

`write_candidate_capture.py:60` `MODULE_ID = "orca-claude-codex-memory-write-trigger-v1"` ✅ ·
`:2237` `values["--bridge-id"] != MODULE_ID` ✅ · `:2330-2336` the SessionEnd no-timeout rationale ✅.

---

## Combined 3-file candidate

`write_candidate_capture.py` + `install_bridge.py` + `claude_memory_hook.py` — **not yet handable**
as a reviewed, deployable-pending-authorization candidate, solely because of P1-A. Nothing found in
this round touches the other two files, and no default-path behavior anywhere is at risk. P1-A is a
two-line change (one parameter, one forward) plus a regression test mirroring the Probe-2 scenario;
once closed, the P2s are all documentation/robustness polish that a human could reasonably accept
as-is with the comment claims corrected to match what the code actually guarantees.
