# Independent Claude opus5/max review — `claude-codex-memory-bridge/install_bridge.py`, round 12

- **Candidate:** `977dac84d4e81ff4fba8e3dd33c7888c9ae8e1e3` (worktree at `881859a5fa`, bridge files byte-identical to the candidate — `git diff 977dac84d4 HEAD -- claude-codex-memory-bridge/` is empty)
- **Baseline compared against:** `366e7035536784ffa311ef31747d78e37acf011d` (round 11)
- **Round-10 baseline for the hybrid regression run:** `6fb376c671`
- **Reviewer:** Claude opus5, effort max, independent read-only review. No file in the candidate was modified; nothing was installed, committed, merged, or activated. Every reproduction ran in `tempfile.TemporaryDirectory()` sandboxes with `install_bridge`'s module constants monkeypatched, or in independent subprocesses. The real `/Volumes/Extreme SSD` Codex `hooks.json` was never read, written, or installed against.
- **Interpreters:** `/usr/bin/python3` 3.9.6 and PATH `python3` 3.14.6 (`/opt/homebrew/bin/python3`). Both used for every finding where the failure mode is interpreter-sensitive.
- **Date:** 2026-08-18

## Verdict: **GO**

No P0 and no P1 found. All three round-11 findings are genuinely fixed, verified with fresh repros that share no account name, no padding shape, no directory mode and no digit count with either the round-11 report or the candidate's own regression tests. The candidate is the first version in this twelve-round cycle where an adversarial pass found no new P0/P1.

Two P2s and two P3s are recorded below. Neither P2 blocks: **P2-R12-A** is a pre-existing, explicitly documented, deliberately-tested residual gap that round 12 *narrows* by 64×; **P2-R12-B** is a deliberate, fail-closed, operator-recoverable trade-off that this round's own reasoning argues for correctly. Both are worth closing in a follow-up, neither is worth blocking on.

---

## Scope note on the process caveat

The dispatch stated the 3-agent adversarial self-check Workflow for this candidate died on an account-level usage limit with zero output, so the candidate reached me with only the implementer's own manual verification. I treated this review as the candidate's first adversarial check and did not lean on any prior agent's conclusion; every claim below rests on a repro I constructed and ran myself. Where the candidate's own commit message or code comments assert a fact, I re-derived it rather than accepting it (see §7 for the two places where an asserted fact did not survive that check).

---

## 1. (a) Independent verification of the three round-11 findings

My repros deliberately differ from both the round-11 report and the candidate's regression tests: accounts are `team-alpha` / `night-shift-07` (not `acct-one`); relocations land in `releases/staging-import/…`, `releases/edge/…`, `releases/quarantine-drop/…`, `backups/rescue/…` (not `backups/misc-staging/…`); padding is a `migration_log` list of timestamped restore-tool records (not `_operator_notes: ["padding"] * 20000`); directory modes are `0o600` and `0o644` (not `0o400`); integer lengths are 12000, 5000, 4300, 4301 digits and a negative variant (not `"7" * 4301`).

### R11-P1-A — bare `ValueError` from CPython's 4300-digit int↔str cap: **FIXED**

Unit level, `_contains_owned_handler()` directly, 12 shapes × 2 interpreters × 2 source versions:

| input | r11 @ 3.14.6 | r12 @ 3.14.6 | r12 @ 3.9.6 |
|---|---|---|---|
| 12000-digit int, unrelated doc, no marker | **BARE ValueError** | `False` | `False` |
| 5000-digit *negative* int | **BARE ValueError** | `False` | `False` |
| exactly 4300 digits (at the limit) | `False` | `False` | `False` |
| 4301 digits (just over) | **BARE ValueError** | `False` | `False` |
| 12000-digit int nested 6 deep in arrays | **BARE ValueError** | `False` | `False` |
| 12000-digit int + marker text in the bytes | **BARE ValueError** | `InstallError` (fail closed) | `False` (no owned handler present) |
| 12000-digit int + *genuine* owned handler | **BARE ValueError** | `InstallError` (fail closed) | `True` (structurally detected) |
| bare top-level 12000-digit int + trailing text | **BARE ValueError** | `False` | `False` |
| utf-16le doc with a 12000-digit int | `False` | `False` | `False` |
| `1e1000000` / `1E999999999999` / 9000-digit fraction | `False` | `False` | `False` |

End-to-end, the exact durable-lockout signature the round-11 report described. Poison = `{"hooks": {"UserPromptSubmit": []}, "seq": <12000 digits>}` at `codex-accounts/night-shift-07/OLD-BACKUP/home/hooks.json`, present **before** `install()`:

```
r11 @ 3.14.6:  [install]  !! InstallError: install failed and the durable recovery journal remains pending
               pending exists = True   latest-receipt exists = True   main config bridged = True
               [plan]    -> ok=False
               [verify]  !! InstallError: pending install journal must be recovered first
               [recover] ###### BARE ValueError: Exceeds the limit (4300 digits) …
               [uninstall] ###### BARE ValueError: Exceeds the limit (4300 digits) …

r12 @ 3.14.6:  [install] -> receipt returned, pending exists = False
               [plan] ok  [verify] ok  [recover] ok  [uninstall] ok=True, main config unbridged
r12 @ 3.9.6:   identical to r12 @ 3.14.6
```

A second end-to-end shape (poison introduced *after* a successful install, at `codex-accounts/team-alpha/ARCHIVED-2025/home/hooks.json`, containing a genuine owned handler plus a 12000-digit `ledger.sequence`): round 11 crashes `uninstall()` with a bare traceback; round 12 raises a clean `InstallError` naming the path, `latest-receipt.json` survives and no pending journal is created. On 3.9.6 the same file is structurally detected and produces the ordinary "does not track" refusal. Both interpreters, both outcomes safe and non-silent.

### R11-P1-B — RUNTIME_BASE tolerance swallowing a Stage-2 marker hit: **FIXED**

Genuine bridged account relocated to `RUNTIME_BASE/releases/staging-import/nightshift/home/hooks.json`, padded with a `migration_log` field, plain canonical UTF-8 JSON, owned handler untouched:

| padded size | round 11 | round 12 |
|---|---|---|
| 106,176 B (~90 KiB) | `uninstall() -> ok:true`, `latest-receipt.json` **deleted**, orphan still live | `InstallError` naming the path; receipt intact; no pending journal |
| 715,176 B (~700 KiB) | `ok:true`, receipt deleted, orphan live | `InstallError` naming the path |
| 3,907,176 B (~3.9 MiB) | (same class) | `InstallError` naming the path |
| **4,194,304 B (== `MAX_MANAGED_FILE_BYTES`)** | — | `InstallError` naming the path — **no off-by-one at the bound** |
| **4,194,305 B (== MAX + 1)** | — | `ok:true`, receipt deleted, orphan live — see **P2-R12-A** |

Independently confirmed that in the round-11 failures the abandoned file really was live: `owned_handler()` returns `True` for its `UserPromptSubmit` entry both before and after the `ok:true` uninstall.

### R11-P2-A — `_is_regular_file()` outside every try block: **FIXED**

Listable-but-unsearchable directory at `RUNTIME_BASE/releases/quarantine-drop/inner`, containing both a `hooks.json` and an unrelated file, at two modes the candidate's own test does not use:

| dir mode | round 11 | round 12 |
|---|---|---|
| `0o600` | `plan` ok, `verify` ok, `recover` ok, **`uninstall` → `InstallError: cannot determine whether …/hooks.json is a managed config`** | all four entry points succeed |
| `0o644` | same hard block | all four entry points succeed |

The corresponding fault **outside** RUNTIME_BASE still fails closed (§5, row "R11-P2-A unsearchable dir OUTSIDE RUNTIME_BASE"), so the fix is a narrowing, not a blanket fail-open.

---

## 2. (b) Hunt for a NEW P0/P1 introduced by this round

### 2.1 Other exception types escaping `_contains_owned_handler()` — none found

29 hand-built pathological inputs × 2 structural bounds (`65536`, `MAX_MANAGED_FILE_BYTES`) × 2 interpreters, plus 1 MiB of seeded random bytes: **0 bare escapes**, on both interpreters. Inputs included 300k unbalanced brackets, 60k balanced nested arrays and objects, a 40k-deep utf-16le nesting, lone-surrogate `\ud800` escapes inside and outside a command string, `NaN`/`Infinity` literals, `1e308999`, a 9000-digit key name, a 6000-digit `timeout` *inside* our own recognized shape, truncated UTF-32/UTF-16 BOMs, `bytes(range(256))*400`, ~4 MiB of `0xff`, empty, whitespace-only, single-quoted JSON, duplicate `hooks` keys, a `--`-escaped marker, a 3 MiB command string, `UserPromptSubmit` as a dict, 200k integers in the handler list, a non-string `command`, trailing garbage after the value, a UTF-8 BOM, and UTF-32BE.

The new broad `except Exception` around Stage 1 **does not change any verdict**. Diffing all 29 verdicts at the `65536` bound between round 11 and round 12 gives a single-line difference on 3.14.6 — the one input where round 11 crashed:

```
- big int as an object VALUE inside our shape [64K] -> ###BARE ValueError
+ big int as an object VALUE inside our shape [64K] -> False
```

and a byte-identical result set on 3.9.6. So the wrapper converts crashes into the documented fall-through and swallows nothing else. `KeyboardInterrupt`/`SystemExit` are `BaseException` and still propagate (verified by injection, §2.2).

I also confirmed by reading that `_contains_owned_handler()`'s remaining unwrapped step, `_raw_bytes_contain_bridge_marker()`, cannot raise (fixed-literal `.encode()` on a module constant plus `bytes.__contains__`), and that `owned_handler()` already catches `shlex.split()`'s `ValueError` itself — so the one obvious "next parser exception" candidate was already closed.

### 2.2 New false tolerance from the broad excepts on `_is_regular_file` / `_read_for_detection` — none reachable

Injection matrix (patching each helper to raise a chosen exception for one specific candidate path):

| helper | raises | inside RUNTIME_BASE | outside RUNTIME_BASE |
|---|---|---|---|
| `_is_regular_file` | custom `Exception` | swallowed → `ok:true` | `InstallError … (at <path>)` |
| `_is_regular_file` | `MemoryError` | swallowed | `InstallError … (at <path>)` |
| `_is_regular_file` | `InstallError` | swallowed | `InstallError … (at <path>)` |
| `_is_regular_file` | `KeyboardInterrupt` | **propagates** | **propagates** |
| `_read_for_detection` | (same four) | (same) | (same) |

That is exactly the intended policy. The widening from `except InstallError` to `except Exception` is unreachable in practice at both call sites: `_is_regular_file()` converts every `OSError` to `InstallError` itself and its only other failure mode (`ValueError` on an embedded NUL) cannot arise from an `os.walk()` name; `_read_for_detection()` likewise funnels every `OSError` into `InstallError`. The residual widening is `MemoryError` on a ≤4 MiB buffer — not reachable at these sizes (measured peak RSS is flat, §2.6). I found no input for which the broad except changes a real-world outcome.

One asymmetry worth recording (not a defect): the `_contains_owned_handler()` call site's `except Exception` re-raises **without** the RUNTIME_BASE tolerance, so it applies the "positive evidence" policy to *any* exception, not only the Stage-2 raise. Because Stage 1 is now internally wrapped, that set is in practice exactly `{the Stage-2 InstallError}`, so the over-breadth is inert today. It would stop being inert if a future change made Stage 2 able to fail for an absence-of-evidence reason.

### 2.3 Defeating the raised bound a new way: **P2-R12-A** (see §4)

Padding a genuine relocated handler past `MAX_MANAGED_FILE_BYTES` entirely reproduces R11-P1-B's exact end state. Boundary measured precisely: 4,194,304 B → caught; 4,194,305 B → silently abandoned. Details, severity reasoning and a fix sketch in §4.

### 2.4 New avoidable lockout from "Stage-2 marker hit always fails closed": **P2-R12-B** (see §4)

A truncated copy of a real bridged `hooks.json` — the natural product of a half-finished restore or `cp` — placed under `RUNTIME_BASE/backups/restore-scratch/hooks.json` carries the marker and does not parse. Round 11 tolerated it; round 12 hard-blocks `uninstall()`, `recover_pending_install()` and `install()`'s commit. Verified fully operator-recoverable. Details in §4.

### 2.5 Real SIGKILL crash recovery with each new mitigation active — no new permanent lockout

Real `SIGKILL` (child exits `-9`, never an in-process exception), delivered from a patched `atomic_write` at a chosen call index. Call ordering was measured first, not guessed: for a 3-config sandbox `install()` issues 14 `atomic_write` calls, #10 being `pending-install.json` and #11–13 the configs, so kill-at-13 leaves a live durable journal with 2 of 3 configs bridged; `uninstall()` issues 4, so kill-at-3 leaves one account reverted and one still installed.

| crash | state injected before recovery | result |
|---|---|---|
| mid-install (#13) | genuine relocated handler >4 MiB under RUNTIME_BASE | `recover` **succeeds**, install rolled back cleanly, no pending journal. (The oversized orphan is silently tolerated — that is P2-R12-A, not a lockout.) |
| mid-install (#13) | genuine 3 MB relocated handler under RUNTIME_BASE | `recover` fails closed naming the path → operator restores path **and** content → `recover` succeeds → clean end state |
| mid-install (#13) | unsearchable dir (`0o600`) under RUNTIME_BASE | `recover` succeeds first try |
| mid-install (#13) | truncated marker-bearing copy under RUNTIME_BASE | `recover` fails closed naming the path → operator deletes it → `recover` succeeds |
| mid-uninstall (#3) | unsearchable dir under RUNTIME_BASE | `recover` succeeds first try |
| mid-uninstall (#3) | truncated marker-bearing copy under RUNTIME_BASE | `recover` fails closed naming the path → operator deletes it → `recover` succeeds |

No scenario ended in a state the documented remediation could not clear. Worth noting for accuracy: in the "3 MB relocated handler" run, restoring only the *path* and not the padded *content* correctly produced `pending install cannot roll back because a config drifted` — the tool refusing to roll back a config whose bytes changed. That is correct behaviour, not a lockout; restoring the content as well recovers cleanly.

### 2.6 (b6) Cost of the raised bound — bounded, and the realistic case is negligible

`_find_untracked_owned_configs()` timings, both interpreters, peak RSS via `getrusage`:

| RUNTIME_BASE contents | 3.9.6 | 3.14.6 |
|---|---|---|
| 12 × 3.9 MiB legitimate **non-candidate** files under `backups/` (the realistic accumulation) | 0.004 s | 0.002 s |
| + one 3.9 MiB **genuine relocated handler** (detected `True`) | 0.029 s | 0.029 s |
| + 6 × 3.9 MiB files literally **named `hooks.json`** (adversarial) | 4.354 s | 3.852 s |

Peak RSS flat across all three (73→75 MB on 3.9.6, 138 MB on 3.14.6). Per-file worst case, isolated:

| 3.9 MiB shape, RUNTIME_BASE bound | 3.9.6 | 3.14.6 | same file at the 64 KiB bound |
|---|---|---|---|
| ~19,500 plain handlers (shlex per handler, `any()` runs to the end) | **1.182 s** | 0.319 s | 0.007 s |
| ~19,500 quote-heavy handlers | 0.866 s | 0.285 s | 0.005 s |
| ~22,000 handlers with unbalanced quotes | 0.519 s | 0.218 s | 0.007 s |
| valid JSON / unparseable junk / NULs / utf-16le | ≤0.044 s | ≤0.273 s | ≤0.023 s |

**Conclusion:** the lock-hold concern from earlier rounds stays bounded. The realistic workload the round-11 tolerance was written for costs ~30 ms. Worst case is ~0.7–1.2 s per multi-MB file *literally named `hooks.json`* under a `0o700` directory this tool owns, scaling linearly, with flat memory. That is acceptable — but it is not "tens of milliseconds", which is what the code comment claims; see **P3-R12-C**.

---

## 3. (c)–(f) Test integrity, suite, regression sweep, call sites

### (c) The 4 new/changed tests are non-vacuous

Hybrid tree: round-11 source (`366e703…`) + round-12 tests.

| test | @ 3.9.6 | @ 3.14.6 | against r12 |
|---|---|---|---|
| `…does_not_crash_on_a_huge_json_integer` | **ok (vacuous here)** | **ERROR** — `ValueError: Exceeds the limit (4300 digits) … value has 4301 digits` | pass |
| `…still_fails_closed_on_a_marker_bearing_file_inside_its_own_runtime_tree` | FAIL — `InstallError not raised` | FAIL — same | pass |
| `…catches_a_large_valid_relocated_handler_inside_its_own_runtime_tree` | FAIL — `InstallError not raised` | FAIL — same | pass |
| `…tolerates_an_unsearchable_directory_inside_its_own_runtime_tree` | ERROR — `PermissionError … → InstallError: cannot determine whether …` | ERROR — same | pass |

Each fails for the *right* reason. The huge-integer test is genuinely vacuous on `/usr/bin/python3` 3.9.6, which has no `sys.get_int_max_str_digits` and no cap — correct and self-documented in the test ("the property under test holds regardless of interpreter"). Because 3.9.6 is the interpreter the generated hook command itself uses, it is worth stating plainly: **R11-P1-A was only ever reachable when the installer was driven by a ≥3.9.14/3.10.7/3.11 interpreter — which is the default `python3` on this machine (3.14.6), so it was genuinely reachable.**

### (d) Full suite — real green on both interpreters

```
/usr/bin/python3        -m unittest discover -s tests -v   →  Ran 94 tests in 1.148s   OK
/opt/homebrew/bin/python3 -m unittest discover -s tests -v →  Ran 94 tests in 1.820s   OK
```
Zero failures, zero errors, zero skips. 39 (`test_claude_memory_hook.py`) + 55 (`test_install_bridge.py`) = 94, and 52 → 55 matches the claimed net change.

### (e) Regression sweep across rounds 1–11

**Hybrid: round-12 source + round-10's own `tests/test_install_bridge.py` (`git show 6fb376c671:…`)** — `Ran 43 tests … OK` on **both** interpreters. No regression in the transaction journal, `owned_handler` exact-match, stat-guard discipline, `prev_*`/`before_*` separation, concurrent-lock discipline, permanently-retired-account carry-forward, pruned-backup-directory tolerance, permission-unreadable fail-closed, or the relocated-account detection shapes round 10 covered.

**Hybrid: round-12 source + round-11's tests** — `Ran 52 tests … FAILED (errors=1)` on both interpreters. The single failure is `test_uninstall_tolerates_an_unrecognizable_file_inside_its_own_runtime_tree`, i.e. *exactly* the test whose premise round 12 deliberately reverses. All 51 others pass. Name-level diff confirms the claimed shape: +3 new tests, 1 replaced (`…tolerates_an_unrecognizable_file…` → `…tolerates_an_unsearchable_directory…`).

**My own 13 independent spot-checks**, all PASS on both interpreters:

| invariant | expected | got |
|---|---|---|
| R8-P1-B relocated one level deeper (subfolder) | refuse | `InstallError` |
| R8-P1-B relocated out of `codex-accounts` entirely | refuse | `InstallError` |
| R9-P1-B relocated + `chmod 0o664` (group-writable) | refuse | `InstallError` |
| R9-P1-B relocated + `chmod 0o644` | refuse | `InstallError` |
| R10 encoding bypass: relocated, re-saved UTF-16LE+BOM | refuse | `InstallError` |
| R10 encoding bypass: relocated, re-saved UTF-32BE | refuse | `InstallError` |
| R10 encoding bypass: relocated + duplicate top-level key | refuse | `InstallError` |
| R8-P2-B unreadable file **outside** RUNTIME_BASE | fail closed | `InstallError: cannot read …` |
| R8-P2-B unlistable dir **outside** RUNTIME_BASE | fail closed | `InstallError: cannot list …` |
| R11-P2-A unsearchable dir **outside** RUNTIME_BASE | fail closed | `InstallError: cannot determine whether …` |
| oversized file **outside** RUNTIME_BASE | fail closed | `InstallError: cannot safely inspect …` |
| unrelated third-party `hooks.json` | not escalated | `ok:true` |
| proven-unrelated config *mentioning* the marker in a shell comment | not escalated | `ok:true` |

Both round-11 P1 fixes still hold: the encoding bypass (rows 5–7 above, plus BOM/UTF-16/UTF-32 marker recognition in the fuzz battery) and the RUNTIME_BASE prune blind spot (§1 R11-P1-B rows, all of which require the walk to enter RUNTIME_BASE at all).

### (f) Call-site audit

| function | call sites | change? |
|---|---|---|
| `_is_regular_file()` | `install_bridge.py:328` (inside `_enumerate_hook_configs`), `:798` (the scan loop) | Only `:798` changed. `:328` is untouched and still fails closed on EACCES — correct, since it is the *managed-shape* enumeration, not the tolerance-bearing safety scan, and it never walks RUNTIME_BASE. |
| `_read_for_detection()` | `:820` only | `except InstallError` → `except Exception`; the `candidate_raw is None` check moved out of the try (same semantics). |
| `_contains_owned_handler()` | `:835` only | New `structural_max_bytes` kwarg; own try block; tolerance removed. Default `_STRUCTURAL_DETECTION_MAX_BYTES` preserves the old behaviour for any hypothetical other caller — there are none. |
| `_find_untracked_owned_configs()` | `:1285` (`recover_pending_install`), `:1797` (`uninstall`) | Unchanged call shape. `install()` reaches the scan transitively through `recover_pending_install()` at `:1662`. |
| `_attempt_structural_detection()` | `:646` only | Signature gained a required keyword-only `max_bytes`. |
| `_safe_parse_strict_utf8()` / `_lenient_*` / `_owned_shape_match()` / `_raw_bytes_contain_bridge_marker()` | inside the detection chain only | No new callers. |

I also verified the code comment's load-bearing invariant myself rather than trusting the "grep-confirmed" annotation: instrumenting `atomic_write` across a real sandboxed `install()` + `uninstall()` shows every file this tool writes under RUNTIME_BASE is `releases/<id>/claude_memory_hook.py`, `releases/<id>/policy.json`, `backups/<id>/<sha256>.json`, `backups/<id>/<sha256>-after.json`, `backups/<id>/receipt.json`, `pending-install.json`, `latest-receipt.json`, `installer.lock` — **never** literally `hooks.json`. `atomic_write()`'s `mkstemp(prefix=f".{path.name}.")` also cannot produce a transient bare `hooks.json`. The invariant holds.

---

## 4. Findings

### P2-R12-A — a genuine relocated handler larger than `MAX_MANAGED_FILE_BYTES` under RUNTIME_BASE is still silently abandoned

**Not introduced by this round** (round 11 tolerated everything from 64 KiB up; round 12 shrinks the exposed band by 64×), explicitly documented in the code, and covered by a deliberate passing test — but it reproduces R11-P1-B's exact end state, so it should be recorded rather than left implicit.

Repro (round-12 candidate, `/usr/bin/python3` 3.9.6 and 3.14.6):

1. `install()` against a sandbox with `.codex` + two accounts.
2. `mv codex-accounts/night-shift-07 → RUNTIME_BASE/releases/staging-import/nightshift` (a naive restore/staging tool preserving filenames — the exact scenario round 11 opened the walk into RUNTIME_BASE for).
3. Pad the relocated `home/hooks.json` with an ordinary top-level field to **4,194,305 bytes** (MAX + 1). Still plain, valid, canonical UTF-8 JSON; the owned handler is untouched and `owned_handler()` returns `True` for it.
4. `uninstall()` → **`{"ok": true}`**, `latest-receipt.json` deleted, main config unbridged — and the orphan handler is still on disk, still live.

Boundary is exact: 4,194,304 B is caught (`InstallError` naming the path), 4,194,305 B is not. Mechanism: `_read_for_detection()` refuses the file at `st_size > MAX_MANAGED_FILE_BYTES` before any content is seen, and the loop's `except Exception` classifies that as absence-of-evidence, which is tolerated inside RUNTIME_BASE. The same gap applies during crash recovery (§2.5 row 1). Outside RUNTIME_BASE the identical file correctly fails closed.

Why this is P2 and not P1: it needs a >4 MiB `hooks.json`, ~64× beyond anything this tool or any plausible hook config produces; the tool itself can never create one (`validate_owned_file()` caps at the same constant); the behaviour is named in the code comment ("an unreadable or **oversized** file … degrade to 'nothing found there'") and pinned by `test_uninstall_tolerates_an_oversized_file_inside_its_own_runtime_tree`; and the alternative — failing closed — reintroduces exactly the permanent-lockout risk in a never-pruned tree that the tolerance exists to prevent.

Suggested follow-up that keeps both properties: for a RUNTIME_BASE candidate that `_read_for_detection()` refuses purely for size, run the **cheap, size-independent** marker search over the file in bounded chunks (`_raw_bytes_contain_bridge_marker()` is already a `bytes in bytes` scan, and a chunked read with a small overlap costs O(size) I/O and no parsing). A hit is positive evidence → fail closed with the path, consistent with this round's own new policy; no hit remains genuine absence of evidence → keep tolerating. That closes the gap without giving oversized debris a permanent veto.

### P2-R12-B — the new "Stage-2 marker hit always fails closed" policy creates a lockout surface for non-live marker-bearing debris under RUNTIME_BASE

Repro: `install()`, then write a truncated copy of a real bridged `hooks.json` (last 12 bytes chopped — what an interrupted `cp`/restore produces) to `RUNTIME_BASE/backups/restore-scratch/hooks.json`. Round 11: `uninstall()` → `ok:true`. Round 12:

```
[uninstall] InstallError: cannot rule out an owned hook handler: content matches the bridge marker
            but does not parse as a recognizable hooks.json under any supported encoding
            (at …/.shared-runtime/claude-codex-memory-bridge/backups/restore-scratch/hooks.json)
```

Same for `recover_pending_install()`. With the debris present *before* `install()`, install writes the pending journal and the configs, then the commit-path `recover_pending_install()` refuses, and install exits with `install failed and the durable recovery journal remains pending`, leaving `pending-install.json` live.

I verified this is **not** a permanent lockout: `recover`/`uninstall` both print the offending path, the file lives in a `0o700` directory owned by this uid, and deleting it makes `recover` commit and `verify`/`uninstall` return to normal — confirmed end-to-end for both the mid-install and mid-uninstall crash variants.

The trade-off itself is right: this tool cannot distinguish "truncated backup copy" from "truncated live handler", and the round is correct that positive evidence must not be discarded. The residual gripe is narrower and is really a UX defect: `install()`'s wrapper message is generic and `main()` prints only `str(exc)` with no `__cause__` chain, so the one path that ends in a durable pending journal is also the one path that does **not** tell the operator which file to remove — they have to run `recover` to find out. Suggested follow-up: include the recovery exception's own text (or at least the path) in the "durable recovery journal remains pending" message.

### P3-R12-C — two code comments now assert things the code does not do

1. `install_bridge.py:733` cross-references "*see `_contains_owned_handler()`'s own comment for the measured cost (tens of milliseconds …)*". `_contains_owned_handler()`'s comment block (lines 574–658) contains **no** measurement — grep for `ms|millisecond|second|cost|measur` returns nothing. The cross-reference is dangling.
2. "Tens of milliseconds" is right for the common shape (~30 ms for a 3.9 MiB genuine relocated handler, where `any()` short-circuits on the first handler) but understates the worst shape by ~40×: **1.182 s** on `/usr/bin/python3` 3.9.6 for a 3.9 MiB document with ~19,500 non-owned handlers, where `owned_handler()`'s `shlex.split()` runs for every one of them. That is precisely the shape the round-2 pressure test measured at 1.0–1.15 s, and the raised bound reintroduces it for RUNTIME_BASE candidates.
3. Relatedly, `_STRUCTURAL_DETECTION_MAX_BYTES`'s own comment (lines 404–419) still says "Bounding BOTH under this one threshold keeps a large … `hooks.json`-named file from turning a routine `uninstall()`/`recover_pending_install()` call into a multi-second-to-minutes exclusive-lock hold". That is no longer true for candidates under RUNTIME_BASE, where the constant is bypassed.

Numbers to substitute are in §2.6. No functional change needed; the measured bound (≤~1.2 s per multi-MB `hooks.json`-named file, linear, flat memory) is acceptable — it should just be stated accurately, because the next round will reason from these comments.

### P3-R12-D — two pre-existing detection gaps, unchanged by this round (recorded so a later round does not mistake them for new)

1. **JSON-escaped marker above the structural bound.** A genuine owned handler whose command is written with the marker JSON-unicode-escaped (`\u002d\u002dbridge-id …`, i.e. the two leading hyphens escaped) parses fine — Stage 1 returns `True` — so it is caught at any size *within* the bound. Above the bound, Stage 2's raw-byte search cannot see the escaped form, so a >64 KiB relocated handler with an escaped marker **outside** RUNTIME_BASE returns a silent `False`. Identical on round 11 and round 12; round 12 actually *fixes* the RUNTIME_BASE half of it (verified: same input returns `False` at the 64 KiB bound and `True` at the 4 MiB bound). Adversarial-only — no real serializer escapes `-` — and anyone able to hand-write that file already has the same-uid access that, per the code's own threat model, defeats this scan regardless.
2. **Duplicate-key first-vs-last divergence.** `_lenient_parse_last_key_wins()` resolves a document with two top-level `"hooks"` keys by taking the last. If the *first* carries the owned handler and the last does not, the scan returns a definitive `False` while a first-wins consumer would still execute the handler. Identical on round 11 and round 12 (verified on both interpreters). Last-wins matches CPython, JavaScript, `serde_json` and `encoding/json` defaults, so the divergence needs an unusual consumer.

---

## 5. What I did not find

- No bare traceback escaping any entry point, on either interpreter, across 29 pathological inputs, 1 MiB of random bytes, 13 regression shapes, 6 SIGKILL scenarios and 12 end-to-end runs.
- No verdict changed by the new broad `except Exception` blocks other than the crashes they exist to convert.
- No regression against round-10's or round-11's own suites beyond the single intentionally-reversed test.
- No new permanent lockout: every fail-closed state I could construct cleared with the remediation the error message names.
- No off-by-one at the raised bound.
- No unbounded lock hold, no memory growth.

## 6. Recommendation

**GO** on `977dac84d4e81ff4fba8e3dd33c7888c9ae8e1e3`. Round 12 fixes all three round-11 findings, is the first candidate in this cycle to survive an adversarial pass without a new P0/P1, and strictly narrows the RUNTIME_BASE blind spot rather than trading it sideways.

Suggested (non-blocking) follow-ups, in priority order:
1. **P2-R12-A** — chunked marker scan for oversized RUNTIME_BASE candidates, fail closed on a hit.
2. **P2-R12-B** — surface the underlying path in `install()`'s "durable recovery journal remains pending" message.
3. **P3-R12-C** — fix the dangling cross-reference and replace "tens of milliseconds" with the measured range; update `_STRUCTURAL_DETECTION_MAX_BYTES`'s comment to say the bound no longer applies under RUNTIME_BASE.

If a future round touches this file again, the process note applies with full force: this file's history is eleven consecutive rounds where a fix introduced a new issue, and the one round that broke the pattern is the one that got a real adversarial pass. Do not let a candidate reach a GO on manual verification alone.
