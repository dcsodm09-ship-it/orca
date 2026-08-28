# Independent review — `claude-codex-memory-bridge/install_bridge.py`, round 11

- **Reviewer:** Claude opus5 / max effort, independent read-only review
- **Date:** 2026-08-18
- **Candidate:** `366e7035536784ffa311ef31747d78e37acf011d` — working tree verified byte-identical to the commit both before and after the review (`git diff --stat 366e7035 -- claude-codex-memory-bridge/` empty; `install_bridge.py` SHA-256 `ab96aabe8144004cd47b842aaacf642e5f80fd4342b73df87f2d4f2a25737c0c` matches `git show 366e7035:…`). Branch HEAD moved during the review, but only from unrelated `prime-agent-integration` commits by another agent; `claude-codex-memory-bridge/` was untouched throughout.
- **Baseline compared against:** `6fb376c671c4edf73047023341757074e91fa255` (round 10)
- **Interpreters:** `/usr/bin/python3` 3.9.6 and `/opt/homebrew/bin/python3` 3.14.6 (the latter is what `#!/usr/bin/env python3` resolves to on this machine)
- **Method:** every repro below was built fresh in this review against isolated temp sandboxes with my own account names (`nx-alpha-7`, `qz-beta-9`), my own relocation depths, and my own encoding-variant bytes. Nothing was re-derived from the task description's examples or from `tests/test_install_bridge.py`. No install/uninstall/recover was ever run against the real `/Volumes/Extreme SSD` Codex configs.

---

## Verdict: **NO-GO**

Both round-10 P1s are **genuinely fixed** — verified end to end with fresh repros on both interpreters. But the round-11 change introduces / leaves open two reproducible P1s and one P2, continuing this file's nine-round pattern of each fix introducing at least one new issue.

| ID | Sev | Summary |
|---|---|---|
| **R11-P1-A** | **P1** | A bare, uncaught `ValueError` escapes the safety scan → bare Python traceback out of `main()`, and (after an `install`) a wedge where every action except `plan` refuses, with **no path named** in the error. Newly reachable inside `RUNTIME_BASE` this round; the new tolerance block does not absorb it. |
| **R11-P1-B** | **P1** | Fix #2 is incomplete: a **live, owned, relocated** config inside `RUNTIME_BASE` is **silently abandoned** whenever it exceeds `_STRUCTURAL_DETECTION_MAX_BYTES` — `uninstall()` returns `ok: true`, the receipt is deleted, the orphan handler stays executable on disk. Identical end-state signature to the bug this round exists to fix. |
| **R11-P2-A** | P2 | The new `RUNTIME_BASE` tolerance wraps `_read_for_detection()`/`_contains_owned_handler()` but **not** `_is_regular_file()`, so a listable-but-not-searchable directory under `RUNTIME_BASE` hard-blocks `uninstall()`/`recover_pending_install()`/`install()`. Recoverable (the error names the path), but the round's own stated invariant for that subtree is not met. |

---

## (a) Both original P1s — independently verified as genuinely fixed

### P1 #1 — encoding bypass (`repro_a.py`)

Four **fresh** encoding variants, none of which is one of the four named in the task description (UTF-8 BOM / duplicate top-level `"hooks"` / trailing `// comment` / UTF-16):

| | variant |
|---|---|
| V1 | UTF-32 **with BOM** (`text.encode("utf-32")`) |
| V2 | UTF-16 **big-endian, no BOM** (`text.encode("utf-16-be")`) |
| V3 | canonical UTF-8 + trailing **binary** junk (`b"\n\x00\x00\x1f\x8b\x08\x00trailing-binary-junk\xff\n"`) |
| V4 | duplicate **nested** `"UserPromptSubmit"` key, **last** occurrence carrying the owned handler |
| V5 | *(false-positive control)* same duplicate nested key, **last** occurrence **not** carrying the handler — last-key-wins is what a real JSON consumer executes, so this account is genuinely **not** bridged and must **not** be flagged |

Flow: real `install()` → `verify()` → relocate `codex-accounts/qz-beta-9` to
`local-homes/cold-archive/2026-Q3/snapshots/qz-beta-9.bak` (four levels deep, renamed at every level) →
re-encode → `uninstall()`.

| | round-10 baseline | round-11 candidate (3.9.6 **and** 3.14.6) |
|---|---|---|
| V1 | `uninstall ok:true`, orphan live | **refused** — "found an owned hook handler at a path the current receipt does not track … `/…/qz-beta-9.bak/home/hooks.json`" |
| V2 | `uninstall ok:true`, orphan live | **refused** (same) |
| V3 | `uninstall ok:true`, orphan live | **refused** — "cannot rule out an owned hook handler … (at `/…/hooks.json`)" (Layer 2 fail-closed) |
| V4 | `uninstall ok:true`, orphan live | **refused** (same as V1) |
| V5 | ok (correct) | **ok (correct — no false positive)** |

**Verified fixed.** The bypass reproduces on the baseline with all four of my own variants (a strictly wider set than the four opus round-10 unit-tested), and none survives on the candidate. V5 confirms the fix did not buy detection at the cost of a false positive.

### P1 #2 — `RUNTIME_BASE` blind spot (`repro_b.py`)

Four **fresh** relocation destinations inside the bridge's own runtime tree, plus one control outside it:

| | destination |
|---|---|
| B1 | `RUNTIME_BASE/backups/20260818T000000.000000Z/stashed-accounts/qz-beta-9` (deep, inside a backups timestamp dir) |
| B2 | `RUNTIME_BASE/releases/<real release_id>/quarantine/qz` (inside the live releases tree) |
| B3 | `RUNTIME_BASE/qz-beta-9` (shallow, directly under `RUNTIME_BASE`) |
| B4 | `local-homes/.shared-runtime/other-tool-staging/qz-beta-9` — **control**, under `.shared-runtime/` but *outside* the bridge subdir |
| B5 | B1 **plus** re-encoded UTF-16LE (both P1s combined) |

| | round-10 baseline | round-11 candidate (both interpreters) |
|---|---|---|
| B1 | `ok:true`, `orphan_still_live=True` | **refused** |
| B2 | `ok:true`, `orphan_still_live=True` | **refused** |
| B3 | `ok:true`, `orphan_still_live=True` | **refused** |
| B4 | refused (control) | refused (control) |
| B5 | `ok:true`, `orphan_still_live=True` | **refused** |

**Verified fixed** for configs within the structural-detection size bound. See **R11-P1-B** for the part that is not.

---

## (b) New-issue hunt

### R11-P1-A — bare `ValueError` escapes the scan → traceback + total wedge  *(P1)*

**What.** CPython ≥ 3.9.14 / 3.10.7 / 3.11 (i.e. the 3.14.6 that `#!/usr/bin/env python3` resolves to here) caps int↔str conversion at 4300 digits. A JSON integer literal longer than that makes `json.loads()` raise a **plain `ValueError`** — *not* a `json.JSONDecodeError`. Every guard in the chain misses it:

| guard | catches | line |
|---|---|---|
| `strict_json()` | `(UnicodeDecodeError, json.JSONDecodeError)` | `install_bridge.py:82` |
| `_safe_parse_strict_utf8()` *(new this round)* | `(InstallError, RecursionError)` | `install_bridge.py:434,436` |
| `_find_untracked_owned_configs()` tolerance *(new this round)* | `InstallError` | `install_bridge.py:753` |
| `main()` | `InstallError` | `install_bridge.py:1905` |

The sibling function added in **the same commit**, `_lenient_parse_last_key_wins()`, *does* catch `ValueError` (`install_bridge.py:482`). The guard was added to Layer 1 and not to Layer 0 — an internal inconsistency in the new code.

**Repro** (`repro_f.py` / `repro_g.py` / `repro_h.py`). An ordinary-looking third-party config, ~4.4 KB:

```python
POISON = ('{"hooks": {"SessionStart": [{"hooks": [{"type": "command",'
          '"command": "/usr/local/bin/other-tool --nonce"}]}]}, "nonce": '
          + "7" * 4301 + '}').encode()
```

dropped at any un-tracked `…/hooks.json` under `local-homes/**`, then a plain `uninstall`.

**Observed, `/opt/homebrew/bin/python3` 3.14.6, candidate:**

| location | round-10 baseline | round-11 candidate |
|---|---|---|
| `local-homes/codex-accounts/nx-alpha-7/legacy-config/hooks.json` | `BARE-ValueError` | `BARE-ValueError` |
| `local-homes/misc-tools/session-nonce-cache/hooks.json` | `BARE-ValueError` | `BARE-ValueError` |
| **`RUNTIME_BASE/backups/staged-restore-2026/hooks.json`** | **`uninstall OK`** (subtree was pruned) | **`BARE-ValueError`** |

On 3.9.6 all rows are `OK` (that interpreter predates the digit limit).

Through the real CLI (`main()`, the same entry point `./install_bridge.py <action>` uses):

```
uninstall -> rc=1  stdout=''  stderr: ValueError: Exceeds the limit (4300 digits) for integer string conversion…
install   -> rc=1  stdout='{"ok": false, "error": "install failed and the durable recovery journal remains pending"}'
```

**Full wedge sequence** (`repro_h.py`, both locations, 3.14.6):

```
after poison + install:  pending=True
install=InstallError(install failed and the durable recovery journal remains…)
  | recover_pending_install=BARE-ValueError
  | verify=InstallError(pending install journal must be recovered first)
  | uninstall=BARE-ValueError
  | install=BARE-ValueError
  | plan=OK
after removing the file: recover_pending_install=OK | verify=OK | uninstall=OK
```

**Why this is P1.**
1. It is a **bare, non-`InstallError` exception escaping `_contains_owned_handler()`**, directly violating the contract this file states twice in comments ("must never be left to raise past this function"; "clean `InstallError`, never a bare traceback"). The identical class — `AttributeError` out of the same scan — was graded **P1** as R8-P1-D.
2. The resulting state is **"every path refused forever except `plan`"** — the exact signature graded P1 as R2-P1-A and R7-P1-A — with a durable pending journal left behind by `install()`.
3. The traceback **names no path**. The path annotation added this round (`install_bridge.py:756`) only decorates `InstallError`, so the operator sees `Exceeds the limit (4300 digits)…` with no indication which file, which directory, or even that a `hooks.json` scan is involved. Compare R11-P2-A, which at least names the offending path.
4. No crash, race, or privilege is required — a single third-party file.
5. **Round-11-specific aggravation:** the third row above. Round 10 pruned `RUNTIME_BASE` and was therefore immune there; round 11 walks in, and its new tolerance block — whose entire stated purpose is that *any* fault in that subtree degrades to "nothing found there" — catches `InstallError` only and lets this straight through.

**Fix sketch.** Add `ValueError` to `_safe_parse_strict_utf8()`'s except clause (matching `_lenient_parse_last_key_wins()`), or widen `strict_json()`'s to `ValueError`; and make `_find_untracked_owned_configs()`'s tolerance/annotation block catch `Exception`, not just `InstallError`, so an unforeseen parser exception in either direction is still annotated with the path rather than escaping raw.

---

### R11-P1-B — fix #2 is incomplete above the structural size bound  *(P1)*

**What.** `_attempt_structural_detection()` returns `None` for anything larger than `_STRUCTURAL_DETECTION_MAX_BYTES` (`install_bridge.py:550`). `_contains_owned_handler()` then falls to Layer 2, finds the marker, and raises `InstallError` (`:616-620`). Inside `RUNTIME_BASE`, `_find_untracked_owned_configs()` swallows that raise (`:753-755`) — **discarding the positive evidence**. The result is exactly the failure mode the round exists to close, gated only on file size.

**Repro** (`repro_j.py`) — the round-11 threat model verbatim, plus one entirely ordinary property:

1. real `install()` across 3 configs;
2. `mv local-homes/codex-accounts/qz-beta-9 → RUNTIME_BASE/backups/20260818T041500.000000Z/pre-migration/qz-beta-9` (a plausible restore/migration staging location);
3. pad that account's `hooks.json` to 136 KB by appending an ordinary `"_operator_notes"` array. **The document stays plain, canonical, strict-valid UTF-8 JSON and the owned handler is untouched** — no encoding trick, no malformation;
4. `uninstall()`.

**Observed, both interpreters:**

```
-- padded to >= 0 bytes --        size=  1154  REFUSED (correct)
     latest-receipt.json = present ; verify() -> ok
-- padded to >= 65537 bytes --    size=136180  uninstall ok=True
     orphan handler still on disk = True | its --bridge-id script still present = True
     latest-receipt.json now = DELETED ; verify() -> not installed: no receipt found
-- padded to >= 200000 bytes --   size=280180  uninstall ok=True   (same)
```

The 1.1 KB control refuses; padding the *same* config past 64 KiB flips it to a silent success. This is the identical end-state the commit message describes for the bug being fixed: *"uninstall() reports {"ok": true, …} while the orphan's real, hash-verifiable, executable handler stays live on disk with no receipt or journal ever referencing it again."*

I re-checked the abandoned handler **after** the successful `uninstall()` to confirm it is genuinely live rather than a dangling reference — `uninstall()` reports `runtime_retained: true`, and every component the orphan command needs is still on disk:

```
uninstall -> {"ok": true, "runtime_retained": true}
POST-UNINSTALL orphan command  : /usr/bin/python3 claude_memory_hook.py --bridge-id orca-claude-native-memory-v1 …
  interpreter exists            : True
  hook script exists            : True
  policy.json exists            : True
  script sha matches --expected-script-sha256 : True
  latest-receipt.json           : DELETED
  pending journal               : absent
```

**A lower-size-bar variant** (`repro_k.py`, ~1.2 KB files, no size condition at all) — three ordinary hand-edit / editor-export shapes applied to the same relocated account:

| shape | outside `RUNTIME_BASE` | inside `RUNTIME_BASE` |
|---|---|---|
| `/* comment */` inside the object | refused | `uninstall ok=True`, receipt **DELETED**, marker still live |
| trailing comma before `]` | refused | `uninstall ok=True`, receipt **DELETED**, marker still live |
| `// header` line prepended | refused | `uninstall ok=True`, receipt **DELETED**, marker still live |

I flag this variant as *supporting* rather than load-bearing: whether the orphan is genuinely executing depends on whether the Codex-side `hooks.json` consumer is lenient (JSONC/trailing-comma tolerant), which I could not establish from this repo. The finding rests on `repro_j.py`, where the file is plain valid JSON under any parser.

**Why this is P1.** Same defect class, same reachability profile (a plain `mv` plus an ordinary file), and the *same* end-state signature as the P1 this round exists to close. It is also not covered by the round's documented residual: that residual explicitly covers only the **outside**-`RUNTIME_BASE` mirror and justifies it as *"a refusal, not silent data loss."* The inside case **is** silent data loss, and is not documented anywhere in the commit message or the code comments.

**Fix sketch.** Under `RUNTIME_BASE`, tolerate only *absence-of-evidence* faults (EACCES, unreadable, oversized-with-no-marker, unrecognizable-with-no-marker) and treat a **Layer 2 marker hit** as a positive detection rather than a tolerated ambiguity. The file's own grep-verified invariant makes this safe: the tool provably never writes a file named literally `hooks.json` under `RUNTIME_BASE` (independently confirmed below), so a marker-bearing `hooks.json` there is never one of its own artifacts. Alternatively, raise the structural bound to `MAX_MANAGED_FILE_BYTES` for candidates under `RUNTIME_BASE` only — measured cost for that is ~40 ms for one 3.7 MiB file (see the lock-hold section).

---

### R11-P2-A — the `RUNTIME_BASE` tolerance misses `_is_regular_file()`  *(P2)*

The new try/except (`install_bridge.py:748-756`) wraps `_read_for_detection()` and `_contains_owned_handler()`. It does **not** wrap `_is_regular_file(candidate)` at `install_bridge.py:723`, which runs earlier in the same loop body and raises `InstallError("cannot determine whether … is a managed config")` on any non-ENOENT `stat()` error.

A directory that is **readable but not searchable** slips through: `os.walk`'s `scandir()` succeeds (so the `onerror` tolerance never fires) and the subsequent `stat(<dir>/hooks.json)` fails with EACCES.

**Repro** (`repro_c.py`) — directory-mode matrix under `RUNTIME_BASE`, candidate, both interpreters:

| dir mode | round-10 baseline | round-11 candidate |
|---|---|---|
| `0o000` | ok | ok (absorbed by `onerror`) |
| `0o100` | ok | ok (absorbed by `onerror`) |
| **`0o400`** | ok | **`InstallError: cannot determine whether …`** |
| `0o500` | ok | ok |
| **`0o600`** | ok | **`InstallError: cannot determine whether …`** |
| `0o700` | ok | ok |

Combined with a real SIGKILL mid-uninstall (`repro_d.py`, candidate):

```
[uninstall kill@3 fault=dir400]
  killed=True pending=True
  | recover=InstallError: cannot determine whether …
  | verify=InstallError: pending install journal must be recovered first
  | install=InstallError: cannot determine whether …
  | uninstall=InstallError: cannot determine whether …
```

Every entry point blocked. Graded **P2** rather than P1 because the error **names the offending path**, so the remedy (`chmod u+x`) is discoverable — unlike R11-P1-A. Note that the round-11 comment's own motivating scenario (a UID mismatch after moving the SSD between machines) *is* correctly handled: that produces neither read nor search permission, so `scandir()` fails and the `onerror` tolerance absorbs it. Only the `r`-without-`x` shape slips.

---

### SIGKILL + each new mitigation — no new lockout (`repro_d.py`)

Real `SIGKILL` in a child process on the Nth `atomic_write`, mid-uninstall (kill@3) and mid-install (kill@4), then each fault planted before recovery:

| planted fault under `RUNTIME_BASE` | recover / install / uninstall after the crash |
|---|---|
| none (control) | all OK |
| 6 MiB `hooks.json` (> `MAX_MANAGED_FILE_BYTES`) | all OK — **tolerated correctly** |
| 200 KiB `hooks.json` holding a real owned handler | all OK — tolerated *(this is R11-P1-B's silent-miss, safe from a lockout standpoint but wrong)* |
| unparseable marker-bearing `hooks.json` | all OK — **tolerated correctly** |
| mode-`0o000` `hooks.json` | all OK — **tolerated correctly** |
| directory at mode `0o400` | **all blocked** — R11-P2-A |

The oversized / unreadable / unrecognizable mitigations do what they claim. Only the directory-mode gap and the ValueError family break recovery.

### "Must never raise past this function" — fuzz + pathological shapes (`repro_e.py`)

~60 targeted pathological inputs (nesting depths 200 → 60 000 as arrays and objects, in UTF-8 / UTF-8-BOM / UTF-16-LE / UTF-16-BOM / UTF-32-BOM; lone surrogates; odd-length UTF-16; huge exponents; 500 duplicate keys; marker-as-key; marker in raw bytes at 5 widths; `NaN`/`Infinity`; unbalanced shell quoting; `hooks` as list/null/dict; non-dict handlers) plus **20 000 randomized fuzz cases** across 5 seeds.

- **3.9.6: 0 contract breaks.**
- **3.14.6: 1 contract break** — the `ValueError` family (R11-P1-A).

No `RecursionError` escapes on either interpreter. Deep nesting resolves either to a definitive `False` (parsed and structurally proven unrelated) or to a fail-closed `InstallError` when the marker is present — the safe direction in both cases.

### False positives / false negatives around the 64 KiB bound (`repro_i.py`)

Third-party content of my own shape: a multi-event config for a fictional `repo-guard` tool whose `changelog` field quotes the bridge marker.

| case | location | size | result | assessment |
|---|---|---|---|---|
| legit, marker mentioned | outside | 829 B | pass through | correct |
| legit, marker mentioned | runtime | 829 B | pass through | correct |
| legit, marker mentioned | outside | 65 454 B | pass through | correct (structurally resolved just under the bound) |
| legit, marker mentioned | outside | 81 954 B | **refused** | **the documented, accepted residual** — verified present, behaves as described |
| legit, marker mentioned | runtime | 81 954 B | pass through | correct |
| legit, **no** marker | outside | 81 967 B | pass through | correct |
| **genuinely owned** | outside | 1 644 B | refused | correct |
| **genuinely owned** | runtime | 1 644 B | refused | correct |
| **genuinely owned** | outside | 65 444 B | refused | correct |
| **genuinely owned** | outside | 81 944 B | refused | correct |
| **genuinely owned** | **runtime** | 81 944 B | **pass through** | **R11-P1-B** |

I also probed the bound byte-exactly with a marker-bearing but structurally-unrelated document: **65 535 / 65 536 bytes → `False`** (structurally resolved, no escalation), **65 537 / 65 538 bytes → `InstallError`** (fail closed). The `>` comparison at `install_bridge.py:550` is off-by-one-clean and the Layer 1 short-circuit is doing its job right up to the edge.

**On the deliberately-not-attempted trade-off:** I think accepting the *outside*-`RUNTIME_BASE` refusal is the right call — it is a loud, path-named refusal on an improbable combination, and closing it would need real machinery. What is **not** right is that the same size bound produces a *silent pass* on the inside, which is the opposite direction and is not documented. The asymmetry, not the residual, is the defect.

### Lock-hold cost — genuinely bounded (`repro_i.py::perf`)

Real files on disk, `_find_untracked_owned_configs()` timed directly plus a full `uninstall()`:

| workload | round-10 baseline (no bound) | round-11 candidate |
|---|---|---|
| 1 × 3.72 MiB `hooks.json` | scan **157 ms** / uninstall 570 ms | scan **42 ms** / uninstall 78 ms |
| 8 × 3.72 MiB | scan **277 ms** / uninstall 418 ms | scan **76 ms** / uninstall 158 ms |
| 24 × 3.72 MiB (89 MiB total) | scan **1 238 ms** / uninstall 3 094 ms | scan **261 ms** / uninstall 524 ms |

The `_STRUCTURAL_DETECTION_MAX_BYTES` bound is doing real work — a ~4.7× reduction at 24 files, with the residual cost dominated by I/O rather than parsing. **The lock-hold concern from earlier rounds is genuinely bounded, not just claimed.** This also means raising the bound *for `RUNTIME_BASE` candidates only* (the R11-P1-B fix sketch) would cost on the order of tens of milliseconds, since the tool writes no `hooks.json`-named file there at all.

---

## (c) Non-vacuity of the 9 new regression tests

Round-11 test file run against the **round-10 module** (`6fb376c671`), test-by-test:

| test | baseline 3.9.6 | baseline 3.14.6 | reason |
|---|---|---|---|
| `…recognizes_encoding_variants_without_crashing` | **FAILED (4)** | **FAILED (4)** | `AssertionError: False is not true` — the real bypass |
| `…does_not_crash_on_pathologically_nested_content` | **ERROR** | OK | `RecursionError` on 3.9; depth 2000 no longer overflows on 3.14 |
| `…does_not_escalate_a_proven_unrelated_config` | OK | OK | baseline never escalated at all |
| `…catches_a_relocated_account_saved_with_a_utf8_bom` | **FAILED** | **FAILED** | `InstallError not raised` |
| `…catches_an_account_relocated_into_the_bridges_own_runtime_tree` | **FAILED** | **FAILED** | `InstallError not raised` |
| `…tolerates_an_unrecognizable_file_inside_its_own_runtime_tree` | OK | OK | baseline pruned the subtree |
| `…tolerates_an_oversized_file_inside_its_own_runtime_tree` | OK | OK | baseline pruned the subtree |
| `…tolerates_an_unreadable_file_inside_its_own_runtime_tree` | OK | OK | baseline pruned the subtree |
| `…still_fails_closed_on_an_ambiguous_file_outside_runtime_base` | **FAILED** | **FAILED** | `InstallError not raised` |

- **5/9 fail on 3.9.6** for the right reason (a bare majority); **4/9 on 3.14.6**. The task's "at least a majority" bar is met on 3.9.6 and narrowly missed on 3.14.6.
- All **9 pass against the round-11 candidate on both interpreters**.
- The three `tolerates_*_inside_its_own_runtime_tree` tests and `does_not_escalate_a_proven_unrelated_config` pass on the baseline **by construction** — the baseline pruned that subtree / never escalated anything. They are legitimate *forward guards* on behaviour this round introduces, but they are not evidence of a defect fixed, and should not be counted as such.
- `does_not_crash_on_pathologically_nested_content` is interpreter-sensitive: its comment claims it "reproduces the crash at Layer 0", which is true on 3.9.6 but **vacuous on 3.14.6** (that interpreter tolerates depth 2000; my sweep shows it needs ≳3000 there). Worth either raising the depth or scoping the comment's claim.

## (d) Full suite — independently run

```
/usr/bin/python3        -m unittest discover -s tests -v   → Ran 91 tests in 2.326s   OK   (exit 0)
/opt/homebrew/bin/python3 -m unittest discover -s tests -v → Ran 91 tests in 2.784s   OK   (exit 0)
```

**91/91 real green on both interpreters, confirmed by direct run.** Grepped the verbose output: 91 `… ok` lines, zero `FAIL`/`ERROR`.

## (e) Rounds 1–10 regression spot-check — clean

1. **Whole round-10 test suite against the round-11 module.** I built a hybrid tree (round-11 `install_bridge.py` + round-10 `tests/`) and ran it: **82/82 OK on 3.9.6 and on 3.14.6.** Every rounds-1-to-10 expectation — transaction journal, `owned_handler` exact-argv match, stat-guard discipline, `prev_*`/`before_*` separation, concurrent lock discipline, permanently-retired-account carry-forward, pruned-backup-directory tolerance, permission-unreadable fail-closed, R6-P1-A already-bridged-adoption refusal, and both SIGKILL crash-recovery classes — still holds.

2. **My own relocation-shape × mode sweep** (`repro_l.py`), 6 shapes × 5 modes = 30 combinations, all on the candidate:

   | shape | 0600 | 0644 | 0664 | 0666 | 0400 |
   |---|---|---|---|---|---|
   | one-level rename | refused | refused | refused | refused | refused |
   | nested under `codex-accounts/` | refused | refused | refused | refused | refused |
   | moved out of `codex-accounts/` | refused | refused | refused | refused | refused |
   | `home/` subdir renamed | refused | refused | refused | refused | refused |
   | into the `.claude/projects` tree | refused | refused | refused | refused | refused |
   | into the `.codex` tree | refused | refused | refused | refused | refused |

   R9-P1-B (mode/owner changed after relocation must still be refused) holds at every shape.

## (f) Anything else new this round

- **Verified the "never named `hooks.json`" claim independently.** Every `atomic_write()` call site under `RUNTIME_BASE` writes `releases/<id>/claude_memory_hook.py`, `releases/<id>/policy.json`, `backups/<ts>/<sha>.json`, `backups/<ts>/<sha>-after.json`, `backups/<ts>/receipt.json`, `latest-receipt.json`, or `pending-install.json` — none is literally `hooks.json`. `atomic_write()` uses `mkstemp(prefix=f".{path.name}.", dir=path.parent)`, so its transient files are `.hooks.json.XXXXXX`, never a bare `hooks.json`. **The claim holds**, and it is exactly what makes the R11-P1-B fix sketch safe.
- **Filesystem-object chaos sweep** (`repro_m.py`): 15 object shapes × 2 locations (a directory named `hooks.json`, a FIFO, a symlink to outside the SSD, a dangling symlink, a symlink to a sibling, empty, truncated-to-zero, exactly at the bound, one over the bound, a setgid subdirectory, UTF-7-encoded marker, embedded NULs, and three big-integer shapes). Clean on 3.9.6; on 3.14.6 the only breaks are the three big-integer shapes (R11-P1-A), in **both** locations.
- **Cosmetic:** `_raise_on_unlistable_directory` (`install_bridge.py:704-709`) produces `cannot list None: …` when `exc.filename` is `None`. Pre-existing, not new.
- **Cosmetic:** the new path annotation at `install_bridge.py:756` duplicates the path for `_read_for_detection()`'s raises, whose message already embeds it (`cannot read /p: … (at /p)`).
- The un-pruned walk now visits every accumulated `backups/<ts>/` and `releases/<id>/` directory. At one `stat()` per directory this is negligible in isolation — measured above — and I found no correctness issue from it beyond R11-P1-A/B/P2-A.

---

## What would clear this

1. **R11-P1-A:** catch `ValueError` in `_safe_parse_strict_utf8()` (matching `_lenient_parse_last_key_wins()` at `:482`), and widen `_find_untracked_owned_configs()`'s tolerance/annotation block at `:753` from `InstallError` to `Exception`, so an unforeseen parser exception is still path-annotated rather than escaping raw. Add a regression test that plants a `hooks.json` with a >4300-digit integer both inside and outside `RUNTIME_BASE` and asserts a clean `InstallError` (not a bare exception) on an interpreter that has the limit.
2. **R11-P1-B:** stop tolerating a **Layer 2 marker hit** under `RUNTIME_BASE` — tolerate only faults that are absence of evidence, not evidence of presence. Add a regression test that relocates a live account into `RUNTIME_BASE` with a `hooks.json` above `_STRUCTURAL_DETECTION_MAX_BYTES` and asserts `uninstall()` refuses.
3. **R11-P2-A:** move `_is_regular_file(candidate)` (`:723`) inside the same tolerance block, or give it the same `_is_under_runtime_root()` degradation. Add a regression test with a `0o400` directory under `RUNTIME_BASE`.

---

## Reproduction artifacts

All under `<scratchpad>/r11/` (isolated temp sandboxes; nothing in the repo was modified):

| file | covers |
|---|---|
| `harness.py` | isolated fake-SSD sandbox, `R11_TARGET=baseline\|candidate` |
| `repro_a.py` | (a) P1 #1 — encoding bypass, 4 fresh variants + 1 false-positive control |
| `repro_b.py` | (a) P1 #2 — 4 fresh `RUNTIME_BASE` relocation destinations + 1 control |
| `repro_c.py` | R11-P2-A — directory-mode matrix inside/outside `RUNTIME_BASE` |
| `repro_d.py` | (b) real SIGKILL × 6 planted faults × all entry points |
| `repro_e.py` | (b) 60 pathological shapes + 20 000 fuzz cases vs the never-raise contract |
| `repro_f/g/h.py` | R11-P1-A — module level, CLI level, and full wedge sequence |
| `repro_i.py` | (b) false-pos/neg matrix around the 64 KiB bound + lock-hold measurement |
| `repro_j.py` | R11-P1-B — clean end-to-end silent abandonment |
| `repro_k.py` | R11-P1-B supporting variant (JSONC / trailing comma / `//` header) |
| `repro_l.py` | (e) 6 relocation shapes × 5 modes |
| `repro_m.py` | (f) filesystem-object chaos sweep |
| `baseline/`, `mix/`, `rev/` | round-10 module; round-11 tests × round-10 module; round-10 tests × round-11 module |
