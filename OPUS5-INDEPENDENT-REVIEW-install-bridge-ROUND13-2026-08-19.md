# Independent Claude opus5/max review — `claude-codex-memory-bridge/install_bridge.py`, round 13

**Candidate:** `0ae1c0eaeadfcc3bf1d2d541acf5b6139b16d3d7`
**Baseline compared against:** `977dac84d4e81ff4fba8e3dd33c7888c9ae8e1e3` (round 12)
**Reviewer:** Claude opus5, effort `max`, independent read-only pass
**Date:** 2026-08-19
**Interpreters:** `/usr/bin/python3` 3.9.6 and `/opt/homebrew/bin/python3` 3.14.6 — every finding and every negative result below was reproduced on both.

---

## Verdict: **GO**

The round-12 gap (opus `P2-R12-A` / Codex `R12-P1-A`) is genuinely and completely fixed. I reproduced it independently on the baseline with my own fresh scenario and confirmed the candidate refuses in all three entry points. The streaming scan itself survived everything I threw at it — I could not find a chunking, overlap, encoding, memory, descriptor, or scoping defect.

I did find **one real regression** introduced by this round: `R13-P2-A`, a pure absence-of-evidence fault class under `RUNTIME_BASE` (a `hooks.json` that is *both* over `MAX_MANAGED_FILE_BYTES` *and* unreadable) now hard-blocks `uninstall()`, `install()` **and** `recover_pending_install()`, where round 12 tolerated it. It is a genuine regression of a property this file explicitly documents at `:769-772` and explicitly tests in two separate places — but it is a **loud, deterministic, path-naming refusal that self-heals completely once the operator acts**, which is this cycle's established P2 shape (identical outcome to `R11-P2-A`, graded P2), not its P1 shape (silent wrong success — `R9-P1-B`, `R11-P1-B`, `R12-P1-A`). So it does not block.

I authored and verified a **2-line fix** for it (§4.4.4); it restores the invariant, keeps the round-13 fix fully intact, and leaves all 97 tests green.

> **Severity dissent axis, stated explicitly so the coordinator can escalate if policy differs from precedent.** A reviewer who treats *any* lockout of the crash-recovery path as automatically P1 — rather than grading by silent-vs-loud as this cycle has consistently done — would rate `R13-P2-A` a P1 and this round a NO-GO. I do not, for the four reasons in §4.4.3, but the disagreement is on the grading rule, not on the facts: the repro is deterministic and takes three lines.

---

## Methodology and independence note

I did not reuse the project's test fixtures, the round-12 reports' examples, or the candidate's own regression tests for any primary verification. I built a separate harness (`harness.py`) that stands up its own isolated fake SSD with different account names (`acct-quebec-77`, `acct-yankee-03`), a different pre-existing handler command (`/bin/echo pre-existing`), a different fake volume UUID, and two accounts instead of one. Nothing ran against the real `/Volumes/Extreme SSD` tree — `install()` was never invoked outside a `tempfile.TemporaryDirectory()`. Scripts live in this session's scratchpad (`…/4108340a-…/scratchpad/`): `harness.py`, `repro_a.py`, `repro_a2.py`, `repro_b1.py` … `repro_b9.py`.

I treated the pre-review self-check Workflow's "SOUND ×2" verdict as a calibration input only, and deliberately probed shapes it reported testing (chunk-boundary fuzz, 150 MiB memory, SIGKILL) with *different* parameters, plus shapes it did not report testing at all (short reads, oversized-∧-unreadable, TOCTOU on the new scan, fd exhaustion, mutation testing of the new tests).

---

## 1. (a) The round-12 gap is genuinely fixed

### 1.1 My scenario, deliberately unlike both prior reports and the candidate's own test

| dimension | candidate's own regression test | **my repro** |
|---|---|---|
| account | `acct-one` | `acct-quebec-77` |
| relocation target | `RUNTIME_BASE/backups/misc-staging/oversized-account` | `RUNTIME_BASE/releases/legacy-import/2019/q4/vault/quebec-home` |
| extra depth | 2 levels | 6 levels |
| padding shape | one giant string under `_operator_notes` | JSON **array** of 100k short strings under `zz_archive_blob` |
| where the padding lands | `_operator_notes` sorts *before* `hooks` → marker near **EOF** | `zz_archive_blob` sorts *after* `hooks` → marker at byte **503**, near **BOF** |
| sizes | `MAX + 16 KiB` | `MAX + 3` (minimum possible overflow) **and** ~9 MiB |

In every variant I asserted the relocated document still parses as valid JSON and that `owned_handler()` says the handler in it is genuinely ours, so the scenario is a real live handler, not marker-shaped debris.

### 1.2 Baseline reproduces the gap exactly

```
=== [baseline_r12] uninstall(), padding MAX+3 (minimum overflow) ===
  size=4194315 (MAX=4194304, over by 11)
  marker byte offset in file = 503
  relocated doc parses as valid JSON: True ; contains a genuinely owned live handler: True
  RESULT: uninstall() returned {"ok": true, "restored": [...]}
  receipt still present after the call: False
  ORPHANED LIVE HANDLER LEFT BEHIND: True
```

Identical at ~9 MiB. `install()` also succeeded silently. Both journal kinds of `recover_pending_install()` returned `ok:true` (`state: committed` / `state: uninstalled`) while abandoning the handler. This is the exact `ok:true` + durable-orphan end-state of `R11-P1-B`, confirming Codex's round-12 characterisation.

### 1.3 Candidate refuses, in all three entry points

```
=== [cand_r13] uninstall(), padding MAX+3 ===
  RESULT: uninstall() raised InstallError: cannot rule out an owned hook handler:
          oversized content under RUNTIME_BASE matches the bridge marker
          (at …/releases/legacy-import/2019/q4/vault/quebec-home/home/hooks.json)
  receipt still present after the call: True
```

| entry point | baseline r12 | **candidate r13** |
|---|---|---|
| `uninstall()` | `ok:true`, receipt deleted, orphan left | **refuses**, receipt preserved, path named |
| `install()` | succeeds silently | **refuses** (`install failed and the durable recovery journal remains pending`) |
| `recover_pending_install()`, `kind="install"` | `ok:true`, `state: committed` | **refuses**, journal + receipt preserved |
| `recover_pending_install()`, `kind="uninstall"` | `ok:true`, `state: uninstalled` | **refuses**, journal + receipt preserved |

Both padding sizes, both interpreters. **(a) verified fixed.**

### 1.4 The refusal is recoverable, not a wedge

After the refusal I shrank the file below `MAX` and re-ran `recover_pending_install()`: it then raises the *correct, pre-existing* round-9/11 error —

```
refusing to finish pending install/uninstall: found an owned hook handler at a path the
current receipt does not track (a managed account directory may have moved -- restore it
to its receipt-recorded path before retrying): …/quebec-home/home/hooks.json
```

— which names the path and the remedy. Restoring the account to its receipt path then completes normally. So the fix converts a silent orphan into a loud, actionable refusal that funnels into the already-correct relocated-account handling.

---

## 2. (b) Hunt for a NEW P0/P1

### 2.1 `_stream_scan_oversized_for_bridge_marker()` — chunking and overlap: **no defect found**

`overlap_len = max(len(variant)) - 1 = 159` (widest variant is UTF-32, 160 B for the 40-char marker). I verified the invariant algebraically (`tail` always holds the last `min(159, consumed)` bytes, which is sufficient because any marker ending in the new chunk has at most `L-1 ≤ 159` bytes behind the boundary) and then attacked it empirically:

| probe | scale | result |
|---|---|---|
| Exhaustive straddle sweep — every offset from `boundary − L − 2` to `boundary + 2`, at boundary 1 MiB **and** 2 MiB, all 5 encodings | 1,090 file constructions | **0 failures** |
| 1-byte-overlap extremes in both directions (marker's last byte is chunk 2's first byte; marker's first byte is chunk 1's last byte), all 5 encodings | 10 | **0 failures** |
| Files at exact chunk multiples (1/2/3 MiB) with marker flush at EOF, flush at BOF, and absent | 33 | **0 failures** |
| **Smallest real input**, `MAX_MANAGED_FILE_BYTES + 1` = 4,194,305 B — i.e. exactly 4 full chunks plus a **1-byte final chunk** — with the marker straddling into that single trailing byte, all 5 encodings | 6 | **0 failures** |
| **Adversarial short reads**: `os.read` monkeypatched to return 7 bytes, then **1 byte**, per call | 7 | **0 failures** |
| Near-miss negatives (truncated marker, `--bridge-ids`, double space, tab) | 5 | correctly `False` |

The 1-byte-per-`os.read` case is the strongest available refutation of an overlap bug and the candidate passes it. Identical results on 3.9.6 and 3.14.6.

The 5 encodings in the streaming scan are byte-identical to `_raw_bytes_contain_bridge_marker()`'s tuple — I diffed them; they are not drifting copies.

### 2.2 Memory, descriptors, cost

```
150 MiB real file, marker at EOF: peak RSS 31.9 MiB -> 31.9 MiB (delta 0.0 MiB)
3 GiB sparse file,  no marker   : peak RSS 31.9 MiB -> 31.9 MiB (delta 0.0 MiB) in 39.5s
=> memory is bounded (not proportional to file size): True
```

Both cases force a full read to EOF. Peak RSS is flat to the resolution of `ru_maxrss`. **Memory-boundedness confirmed at 3 GiB**, well past the 150 MiB the pre-review Workflow reported.

**No descriptor leak:** 1,200 mixed scans (early-return-`True`, read-to-EOF-`False`, `EACCES`-on-open, `ENOENT`-on-`lstat`) left `/dev/fd` unchanged at 5. The `descriptor = -1` sentinel plus `finally` is correct on every path, including the `path.lstat()`-raises path where no descriptor was ever opened.

**Lock-hold cost is real but linear** — see `P3-R13-C` in §4.

### 2.3 The `RUNTIME_BASE` / outside-`RUNTIME_BASE` split is correctly scoped

All four required combinations, both interpreters:

| candidate file | round 12 | **round 13** | required? |
|---|---|---|---|
| **in** `RUNTIME_BASE`, oversized, **with** marker | tolerated (`ok:true`) | **refused** — `cannot rule out an owned hook handler: oversized content under RUNTIME_BASE matches the bridge marker (at …)` | ✅ the fix |
| **in** `RUNTIME_BASE`, oversized, **no** marker | tolerated | **tolerated** | ✅ unchanged |
| **outside**, oversized, **with** marker | refused — `cannot safely inspect …: too large (at …)` | **refused — same original "too large" message** | ✅ *not* routed through the new path |
| **outside**, oversized, **no** marker | refused — `too large` | **refused — `too large`** | ✅ unchanged |

The outside-`RUNTIME_BASE` re-raise is `raise InstallError(f"{exc} (at {candidate_str})") from exc` — **byte-identical** to the baseline's `except Exception` re-raise, and it is evaluated *before* the streaming scan, so a marker-bearing oversized file outside `RUNTIME_BASE` provably never reaches the new code. Verified both by reading and by the message assertion above.

Clause ordering is correct: `except _CandidateTooLargeForDetection` precedes `except Exception`, which is required since it is an `InstallError` subclass.

### 2.4 TOCTOU on the new scan

Injected mutations between `_read_for_detection()`'s size check and the streaming scan:

| race | outcome |
|---|---|
| file deleted before the scan | refused (`cannot read …`) — see `R13-P2-A`; transient, retry succeeds |
| replaced by a **small owned handler** | refused (marker found) — fail-closed, correct |
| replaced by a **directory** | tolerated (scan's `S_ISREG` re-check returns `False`) — same as baseline |
| replaced by a **symlink to an owned handler** | refused (`O_NOFOLLOW` → `ELOOP`) — fail-closed, correct |
| `chmod 000` just before the scan | refused — see `R13-P2-A` |

No race produces a **silent pass** where the baseline refused, and none produces a false *negative* on a marker-bearing file. The `O_NOFOLLOW` protection is genuinely equivalent to `_read_for_detection()`'s.

### 2.5 Real SIGKILL in the crash-recovery window — **no new permanent lockout**

Real `SIGKILL` (`returncode == -9`, verified) at `atomic_write` call #3 of `uninstall()`, with the oversized genuine relocated handler planted **after** the crash so it is present precisely during the recovery window:

```
  child returncode=-9        durable pending journal survived: True
  recover_pending_install() REFUSED: cannot rule out an owned hook handler: … (at …)
  journal still pending: True   (state preserved, retry possible)
  AFTER operator restores the account: -> {"ok": true, "state": "uninstalled"}
  journal cleared: True   NO PERMANENT LOCKOUT: True
```

Baseline in the same scenario: `ok:true`, `state: uninstalled`, **orphan left behind**.

The mirror ordering (handler planted **before** the crashing `uninstall()`) is also correct on the candidate: `uninstall()` refuses at the safety scan *before* writing any journal, so the child exits 1 rather than being killed mid-transaction, the receipt is preserved, and the install stays intact. Nothing is orphaned by a false success in either ordering.

CLI hygiene: `main()` catches the new error cleanly — `rc=1`, single-line `{"ok": false, "error": …}` JSON on stdout, **no bare traceback** on stderr, both interpreters, for both the marker-hit and the `R13-P2-A` shapes.

### 2.6 Call-site audit for the new exception subclass

```
$ grep -n '_read_for_detection('        install_bridge.py
214:def _read_for_detection(path: Path) -> bytes | None:
893:            candidate_raw = _read_for_detection(resolved)          # the ONLY call site
$ grep -n '_CandidateTooLargeForDetection' install_bridge.py
63:class _CandidateTooLargeForDetection(InstallError):
894:        except _CandidateTooLargeForDetection as exc:               # the ONLY catch site
```

(Other hits are comment prose only.) The test suite never references `_read_for_detection` or the new class outside comments. `_read_for_detection()` has exactly one caller and the new subclass exactly one handler, so **the subclass change cannot alter behaviour anywhere except the one intentionally-modified site**. Every other handler in the file catches `InstallError` or `Exception`, both of which still match. `main()`'s `except InstallError` still catches it (it is a subclass), so no path can leak a raw traceback.

### 2.7 What the marker-only scan cannot see above `MAX` (residual, **not** a regression)

Above `MAX`, only the literal marker is searched — structural detection is impossible by construction. So a *structurally* owned handler whose raw bytes lack the literal marker is missed:

| command form | `owned_handler()` | literal marker in file | detected above `MAX` |
|---|---|---|---|
| `--bridge-id orca-…-v1` (**what the bridge itself writes**) | True | True | **True** |
| `--bridge-id 'orca-…-v1'` (quoted) | True | False | False |
| `--bridge-id␣␣orca-…-v1` (two spaces) | True | False | False |
| `--bridge-id\torca-…-v1` (tab) | True | False | False |

I checked whether the bridge can ever emit those forms: `make_release()` builds the command as `" ".join(shlex.quote(part) for part in command_parts)`, and `shlex.quote("orca-claude-native-memory-v1")` returns the string unquoted (it is `[a-z0-9-]` only). **The bridge never writes any form the scan misses.** Reaching this residual requires hand-editing the relocated config, which per the file's own threat model already implies the same-uid access that defeats this scan anyway. Recorded as `P3-R13-D`; it is strictly narrower than the baseline, which missed *every* oversized case.

---

## 3. (c) The 3 new/changed tests are non-vacuous

### 3.1 Against the round-12 baseline (hybrid: baseline `install_bridge.py` + round-13 tests)

```
test_stream_scan_oversized_finds_a_marker_split_exactly_across_a_chunk_boundary ... ERROR
    AttributeError: module 'install_bridge' has no attribute
                    '_stream_scan_oversized_for_bridge_marker'
test_uninstall_catches_an_oversized_valid_relocated_handler_inside_its_own_runtime_tree ... FAIL
    AssertionError: InstallError not raised
test_uninstall_still_fails_closed_on_an_oversized_file_outside_runtime_base_even_with_a_marker ... ok
```

Both fix-verifying tests fail **for the right reason**; the third passes on baseline, exactly as the candidate claims for a regression-guard control.

### 3.2 Mutation testing (the stronger check the baseline run cannot give)

An `AttributeError` on baseline proves only that the function is new, not that the test discriminates a *correct* implementation from a *broken* one. So I mutated the candidate:

| mutation applied to the candidate | test that should die | result |
|---|---|---|
| `overlap_len = max(...) - 1` → `overlap_len = 0` (kills the sliding window) | `…marker_split_exactly_across_a_chunk_boundary` | **FAILED** ✅ |
| `if marker_found:` → `if False and marker_found:` (neuters the fix) | `…catches_an_oversized_valid_relocated_handler…` | **FAILED** ✅ |
| `if not _is_under_runtime_root(resolved):` → `if False:` (routes outside-`RUNTIME_BASE` through the new scan) | `…still_fails_closed_…outside_runtime_base…` | **FAILED** ✅ |

All three tests are genuinely load-bearing, including the control.

**One test-quality nit** (`P3-R13-E`): `test_stream_scan_…` hard-codes `chunk_size = 1_048_576` instead of reading it from the module. If a later round changes the chunk size, the constructed offset stops straddling and the test silently degrades to a non-straddling positive — it would still pass while testing nothing. Reading the constant from the module (or asserting it) would close that.

---

## 4. (d) Full suite, and (e) regression sweep

### (d) Real green — verified myself, both interpreters

```
/usr/bin/python3 (3.9.6)          : Ran 97 tests in 7.588s -- OK   (exit 0)
/opt/homebrew/bin/python3 (3.14.6): Ran 97 tests in 4.298s -- OK   (exit 0)
```

97 = 58 (`test_install_bridge.py`, up from 55) + 39 (`test_claude_memory_hook.py`, untouched by this commit). The three added test names diff exactly as claimed; the commit touches exactly 2 files (+196/−5).

### (e) Regression sweep

**Hybrid tree — round-13 `install_bridge.py` + round-10's own `tests/test_install_bridge.py`** (`git show 6fb376c671:…`, 43 tests):

```
3.9.6  : Ran 43 tests in 3.459s -- OK
3.14.6 : Ran 43 tests in 2.008s -- OK
```

No regression against rounds 1–10's fixed items.

**Round-11 findings, independently re-verified on this candidate** (own repros, not the repo's tests):

| finding | probe | result |
|---|---|---|
| `R11-P1-A` — bare `ValueError` from CPython's 4300-digit int↔str cap | `hooks.json` with a 4301-digit integer, planted **inside and outside** `RUNTIME_BASE`, on **3.14.6** (where the limit exists) and 3.9.6 | **holds** — no bare exception on any of the 4 combinations |
| `R11-P1-B` — live relocated handler above `_STRUCTURAL_DETECTION_MAX_BYTES` | 262,155 B relocated handler under `RUNTIME_BASE` (> 65,536, < `MAX`) | **holds** — `uninstall()` refuses with the relocated-account error |
| `R11-P2-A` — `_is_regular_file()` outside the tolerance block | `0o400` (readable, non-searchable) directory under `RUNTIME_BASE` | **holds** — tolerated, `ok:true` |

---

## 4.4 Findings

### `R13-P2-A` — a file that is **both** oversized **and** unreadable under `RUNTIME_BASE` now hard-blocks all three entry points *(P2 — new regression, non-blocking)*

#### 4.4.1 Repro (deterministic, 3 lines of setup, no race, no adversary, no privilege)

```python
i.install()                                              # healthy, fully installed machine
blob = RUNTIME_BASE / "backups/2026-05-02T00-00-00Z/quarantine/hooks.json"
blob.write_bytes(b"\x00" * (MAX_MANAGED_FILE_BYTES + 1))  # 4 MiB + 1
os.chmod(blob, 0o000)                                     # e.g. left behind by a `sudo cp`
```

No marker. No owned handler. No JSON. Pure absence of evidence.

```
########## baseline_r12 ##########                    ########## cand_r13 ##########
uninstall()               -> ok                        uninstall()               -> BLOCKED: cannot read …/quarantine/hooks.json
install()                 -> ok                        install()                 -> BLOCKED: install failed and the durable
                                                                                              recovery journal remains pending
pending journal present: False                         pending journal present: True   <-- left mid-transaction
recover_pending_install() -> ok                        recover_pending_install() -> BLOCKED: cannot read …/quarantine/hooks.json
--- operator chmods 0600 ---                           --- operator chmods 0600 ---
recover_pending_install() -> ok                        recover_pending_install() -> ok  {"state": "committed"}
```

Identical on 3.9.6 and 3.14.6.

#### 4.4.2 Mechanism

`_read_for_detection()` checks `st_size > MAX_MANAGED_FILE_BYTES` **before** `os.open()`, so for an oversized-*and*-unreadable file the size raise wins and the unreadability is never observed. Round 12 blanket-tolerated that raise under `RUNTIME_BASE`, so the combination was tolerated. Round 13 routes it into the streaming scan, whose own `os.open()` then fails `EACCES` → `InstallError("cannot read …")` → and the call site's

```python
except Exception as scan_exc:
    raise InstallError(f"{scan_exc} (at {candidate_str})") from scan_exc
```

converts a genuine *absence of evidence* into a hard refusal. It is not limited to `EACCES`: the file vanishing mid-scan, `EIO`, and the identity-changed-mid-scan raise all take the same route (§2.4).

This contradicts the file's own invariant, which round 13 did **not** update (`install_bridge.py:769-772`, still asserting the pre-round-13 behaviour):

> *"EXCEPT specifically inside RUNTIME_BASE, where an unlistable directory, an unsearchable directory (`_is_regular_file()`'s own raise), and **an unreadable or oversized file** (`_read_for_detection()`'s own raises) all instead degrade to 'nothing found there', same as ENOENT"*

…and the reason it gives for that invariant is precisely the outcome now reachable:

> *"…would permanently hard-block BOTH `uninstall()` and `recover_pending_install()` (the tool's own crash-recovery path) with no self-healing action available, since RUNTIME_BASE content is never pruned by this tool."*

The suite holds each half separately — `test_uninstall_tolerates_an_unreadable_file_inside_its_own_runtime_tree` (a **33-byte** file at `0o000`) and `test_uninstall_tolerates_an_oversized_file_inside_its_own_runtime_tree` (a **readable** `MAX+1` file) — so nothing covers their conjunction, which is why 97/97 stays green.

#### 4.4.3 Why P2 and not P1

1. **Loud, not silent.** The refusal is deterministic and names the offending absolute path. Nothing is orphaned, no receipt is deleted, no `ok:true` is ever returned. This cycle's P1 bar has consistently been *silent wrong success* (`R9-P1-B`, `R11-P1-B`, `R12-P1-A`); this is the opposite failure direction.
2. **Direct precedent.** `R11-P2-A` was the identical outcome — an absence-of-evidence fault under `RUNTIME_BASE` hard-blocking all three entry points, recoverable because the error names the path — and was graded **P2** by this same review path. Grading this P1 would be inconsistent.
3. **Verified full self-heal.** `chmod 0600` (or `rm`) on the named path restores every entry point, including finishing a pending journal — I ran it end-to-end.
4. **Narrower reach than it first looks.** Round 11's own analysis established that the motivating "UID mismatch after moving the SSD between machines" scenario strips *both* read and search permission, so `os.walk`'s `onerror` tolerance absorbs it before the file is ever `stat`ed. This needs a file specifically unreadable inside a still-readable, still-searchable directory — realistic (a `sudo cp` of a large config into the never-pruned `backups/` tree; a root-owned quarantine copy) but not the common case.

Aggravating, and why I would not grade it below P2: it reaches the **crash-recovery path**, it leaves a **pending journal** behind when hit through `install()`, `RUNTIME_BASE` is never pruned so debris accumulates for the tool's lifetime, and the file's own comment now states the opposite of what the code does.

#### 4.4.4 Fix — verified, 2 lines

Scan failure is the definition of "we could not read its bytes", i.e. exactly the class the broader handler already tolerates:

```python
            try:
                marker_found = _stream_scan_oversized_for_bridge_marker(resolved)
-           except Exception as scan_exc:
-               raise InstallError(f"{scan_exc} (at {candidate_str})") from scan_exc
+           except Exception:
+               # The scan itself failing (EACCES, EIO, the file vanishing
+               # mid-scan) IS genuine absence of evidence -- the same class
+               # the broader handler below tolerates under RUNTIME_BASE.
+               continue
```

Verified in a scratch copy (the repo was not modified):

* full suite **58/58 green on 3.9.6 and 3.14.6**;
* scoping matrix: `[IN RUNTIME_BASE] OVERSIZED + unreadable` back to **TOLERATED**, every other row **unchanged**;
* the round-13 fix still fully intact — my §1 repro still refuses with the marker-hit message in `uninstall()`, `install()` and `recover_pending_install()`;
* the minimal `R13-P2-A` repro returns to baseline behaviour on all three entry points.

The `:769-772` comment should be amended in the same change to say that an oversized file under `RUNTIME_BASE` is now *scanned*, and only a marker hit fails closed.

---

### `P2-R12-B` — **still open, and this round widens it** *(inherited, non-blocking)*

Round 12 recorded that "a Stage-2 marker hit always fails closed" creates a lockout surface for non-live marker-bearing debris under `RUNTIME_BASE`, and that the one path ending in a durable pending journal — `install()` — is the one path that does **not** name the offending file (`install failed and the durable recovery journal remains pending`, with `main()` printing only `str(exc)` and no `__cause__` chain).

Round 13 deliberately and correctly extends the same policy to oversized marker-bearing debris, so the surface is now wider, and I hit the identical UX defect in every `install()` repro in §1.3 and §4.4.1. Round 12's suggested follow-up (include the recovery exception's text or at least the path in that message) is unimplemented and is now worth more.

### `P3-R13-C` — the lock-hold comment is now further out of date, and the new scan's cost is unbounded in file size

`_STRUCTURAL_DETECTION_MAX_BYTES`'s comment (`:425-441`) still claims the bound "keeps a large, unrelated, or deliberately oversized `hooks.json`-named file from turning a routine `uninstall()`/`recover_pending_install()` call into a multi-second-to-minutes exclusive-lock hold", and that a file over the bound "skips straight to Layer 2's byte-pattern marker check, which stays cheap **regardless of size**". Under `RUNTIME_BASE` that is now doubly untrue: round 12 bypassed the bound up to `MAX`, and round 13 makes an over-`MAX` file cost a **full sequential read of the entire file** while holding the exclusive installer lock.

Measured on the candidate (baseline: 0.01–0.02 s, i.e. instant `continue`):

| debris under `RUNTIME_BASE` | round 12 | **round 13** |
|---|---|---|
| 1 × 64 MiB oversized, no marker | 0.01 s | **1.69 s** (~38 MiB/s) |
| 8 × 8 MiB oversized, no marker | 0.02 s | **1.73 s** |
| 3 GiB (isolated scan) | n/a | **39.5 s** (~77 MiB/s) |

Linear, flat-memory, and an unavoidable consequence of the fix both round-12 reviewers asked for — so no functional objection. But the cost is now unbounded in file size (a 10 GiB stray would hold the lock ~4 min), and the next round will reason from these comments. This continues `P3-R12-C`, whose item 1 — the dangling *"see `_contains_owned_handler()`'s own comment for the measured cost (tens of milliseconds…)"* cross-reference to a comment block containing no measurement — is **also still unfixed** on this candidate.

### `P3-R13-D` — marker-only detection above `MAX` misses shlex-equivalent-but-not-literal marker forms

See §2.7. Unreachable by anything the bridge writes (`shlex.quote` leaves the id bare); strictly narrower than the baseline. Recorded so a later round does not mistake it for new.

### `P3-R13-E` — `test_stream_scan_…` hard-codes the chunk size

See §3.2. Would silently stop testing the straddle if `chunk_size` ever changes.

---

## 5. What I did not find

* No chunking, overlap, or encoding defect in `_stream_scan_oversized_for_bridge_marker()` across 1,151 constructed inputs, including 1-byte-at-a-time short reads and the minimum real input (`MAX + 1`, a 1-byte final chunk).
* No memory growth: flat `ru_maxrss` at 150 MiB and 3 GiB.
* No file-descriptor leak across 1,200 mixed scans covering all four exit paths.
* No mis-scoping: the outside-`RUNTIME_BASE` branch is evaluated before the scan and re-raises the byte-identical baseline message.
* No behaviour change at any `_read_for_detection()` call site other than the one intentionally modified — there is only one call site and only one catch site.
* No silent pass introduced by any TOCTOU race I could inject; every race resolves fail-closed or matches baseline.
* No new permanent lockout: every fail-closed state I constructed, including `R13-P2-A`, cleared with the remediation the error names — verified end-to-end through a real `SIGKILL` recovery.
* No bare traceback from any entry point on either interpreter.
* No regression against round-10's suite (43/43) or against `R11-P1-A` / `R11-P1-B` / `R11-P2-A`.
* No vacuous test among the three added: all three die under targeted mutation.

---

## 6. Recommendation

**GO** on `0ae1c0eaeadfcc3bf1d2d541acf5b6139b16d3d7`. The round-12 gap that both prior reviewers found — and disagreed only on how to label — is genuinely closed, in all three entry points, at any file size, with bounded memory, and without weakening the outside-`RUNTIME_BASE` fail-closed policy. This is the second consecutive candidate to survive an adversarial pass with no new P0/P1, and the first to do so on the actual fix that has been contested since round 11.

Non-blocking follow-ups, in priority order:

1. **`R13-P2-A`** — apply the verified 2-line `continue` (§4.4.4) and amend the `:769-772` invariant comment. Add the missing conjunction test (`MAX+1` bytes at `0o000` under `RUNTIME_BASE` → tolerated), which is the one shape the suite's two existing tolerance tests do not cover between them.
2. **`P2-R12-B`** — include the recovery exception's own text (or at least the path) in `install failed and the durable recovery journal remains pending`. This round widened the set of inputs that land there.
3. **`P3-R13-C`** — correct the `_STRUCTURAL_DETECTION_MAX_BYTES` and `_contains_owned_handler()` cost comments with the numbers in §4/`P3-R13-C`, and fix round 12's still-dangling "measured cost" cross-reference.
4. **`P3-R13-E`** — read `chunk_size` from the module in the chunk-boundary test instead of hard-coding `1_048_576`.

---

### Appendix — evidence index

| script | covers |
|---|---|
| `harness.py` | isolated fake-SSD fixture, independent of the repo's test fixtures |
| `repro_a.py` | (a) fresh end-to-end gap repro — `uninstall()` / `install()` / `recover_pending_install()`, `MAX+3` and ~9 MiB, marker-near-BOF padding shape |
| `repro_a2.py` | (a) both journal kinds; refusal → operator remediation → self-heal |
| `repro_b1.py` | (b) 1,155-input streaming-scan fuzz: straddle sweep, 1-byte extremes, exact multiples, `MAX+1`, short reads, near-miss negatives |
| `repro_b2.py` | (b) 4-cell scoping matrix + 6 absence-of-evidence combinations → **`R13-P2-A`** |
| `repro_b3.py` | (b) TOCTOU injections, fd-leak check, memory boundedness at 150 MiB / 3 GiB |
| `repro_b4.py` | (b) real `SIGKILL` mid-uninstall, handler planted before and after the crash |
| `repro_b5.py` | (b)/(e) crash detail + `R11-P1-A` / `R11-P1-B` / `R11-P2-A` re-verification |
| `repro_b6.py` | (b) CLI-level error hygiene + lock-hold cost measurement |
| `repro_b7.py`, `repro_b8.py` | (b) minimal deterministic `R13-P2-A` repro across all three entry points |
| `repro_b9.py` | (b) residual detection scope above `MAX` (`P3-R13-D`) |
| `hyb_c/`, `hyb_e/`, `mut/`, `fixprop/` | (c) baseline non-vacuity + 3 mutations; (e) round-10 hybrid; verified 2-line fix |
