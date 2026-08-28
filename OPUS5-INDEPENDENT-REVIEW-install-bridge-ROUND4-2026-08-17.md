# Independent read-only review — `install_bridge.py` round 4

- **Reviewer**: Claude opus5 / max effort, independent worker (`task_5e69af064658`), blind to the parallel round-4 reviewer
- **Candidate**: `d80d14c517` — `claude-codex-memory-bridge/install_bridge.py` + `tests/test_install_bridge.py`
- **Round-3 baseline for the delta**: `870810d468` (also used `09459cf6a0` as the round-1/2 reference)
- **Date**: 2026-08-17
- **Mode**: read-only. Nothing was modified, committed, merged, installed or activated in the repository or on the real machine. Every reproduction ran in throwaway sandboxes under the session scratchpad against a **fake** SSD root (`<scratch>/r4rig-*/EXTSSD`) with monkeypatched module constants. **No `install`/`uninstall`/`recover` was ever run against the real `/Volumes/Extreme SSD` Codex hook configs.** Verified untouched at the end of the review: `/Volumes/Extreme SSD/Orca/local-homes/.shared-runtime` still does not exist, and the real `~/.codex/hooks.json` still has exactly 1 `UserPromptSubmit` handler with no `orca-claude-native-memory-v1` mention.
- **Candidate isolation**: before touching anything I exported the candidate with `git archive d80d14c517` to `<scratch>/cand-d80d14c` and confirmed byte-identity with the worktree (`install_bridge.py` sha256 `fad1831788ca…a12c`). **All reproduction ran against that pinned export**, so worktree movement cannot affect any conclusion here.
- **Drift disclosure**: at review start `HEAD` was `d80d14c517`. At review end `HEAD` had moved to `652e19cb77` ("docs(backlog): record install_bridge.py round-3 dual NO-GO + round-4 fix/dispatch"). I checked it: `d80d14c517` is an ancestor of `652e19cb77`, and `git diff d80d14c517 HEAD -- claude-codex-memory-bridge/` is **empty** — the entire reviewed directory is byte-identical. Both reviewed files still hash to their candidate values. No conclusion is affected and no `git archive` fallback was needed mid-run (I had already started from one).
- **Harness**: written from scratch for this round (`<scratch>/rig/harness.py`, `exp_a_retired.py`, `exp_a2_lockout_matrix.py`, `exp_b_temp_rename.py`, `exp_c_pruned_backups.py`, `exp_d_interrupt.py`, `exp_e_schema.py`, `exp_f_regressions.py`, `exp_g_adversarial.py`, `exp_g2_followup.py`, `exp_h_absent_conflation.py`, `exp_h2_cli.py`). Deliberately unlike the candidate suite's fixture: **4** managed configs instead of 2, UUID-shaped account directories (`7f3c1a02-aaaa-…`, `91ab55d7-bbbb-…`, `c0de99e4-cccc-…`) matching the *real* machine's layout rather than `acct-one`/`acct-two`, **distinct pristine bytes per config** and **distinct pristine modes per config** (`0o644`/`0o600`/`0o640`/`0o604`) so that "restored to pristine" is checked byte+mode exact per path and a cross-path mix-up cannot pass, and interruption points chosen **semantically** (by which file is about to be written) rather than by `atomic_write` call number.

---

## Verdict: **NO-GO**

All four fixes this round claims are **genuinely fixed**, and I confirmed each is non-vacuous by reproducing the original defect on the prior candidate with my own fixtures:

| Claimed fix | Verdict | Non-vacuity evidence |
|---|---|---|
| ① R3-P1-A / P1-R3-1 — permanently retired account | **FIXED** | Same repro on `870810d468` yields the full lockout; on `09459cf6a0` the row is silently dropped |
| ② R3-P2-A — pruned prior-transaction backup dir | **FIXED** | Same repro on `870810d468` fails `uninstall` with `path unavailable` |
| ③ P2-R3-TEST — interrupted-upgrade production behaviour | **FIXED / already correct** | Same repro on `09459cf6a0` leaves 3 configs stuck at v2 with an unrecoverable journal |
| ④ P2-R3-COMPAT — receipt schema v1→v2 | **FIXED** | Round-1-shaped receipt on `870810d468` yields the indistinguishable `invalid receipt config row` |

But **this round introduces a new P1 of its own**, and it is a functional regression against `870810d468`:

| ID | Introduced by | Effect | Repro needs |
|----|---------------|--------|-------------|
| **R4-P1-A** | fix ① (the new `"absent"` state) | The `"absent"` decision is made with `Path.exists()` / `Path.is_symlink()`, which **cannot distinguish "genuinely deleted" (`ENOENT`) from "cannot be stat'd" (`EACCES`, `EIO`, …)**. A managed config that still exists on disk and still contains the live bridge handler gets classified `"absent"`. On Python ≥ 3.12 `uninstall` then returns **`ok: true`**, deletes `latest-receipt.json`, and leaves the bridge permanently active in that config with the true baseline lost forever (P1-1 resurrected). On Python 3.9/3.11 the same input escapes `main()` as a **bare `PermissionError` traceback** instead of the documented `{"ok": false, …}`. Round 3 failed **closed** on the identical input and recovered completely. | one `chmod` on an account's `home` directory. No crash, no race, no privilege. |

This is the **fifth consecutive round** in which the round's own fix introduces a new defect, and the third in a row where the new defect is in the *same* code the round was fixing. Notably the failure *direction* has now flipped: rounds 1–3 introduced **fail-closed** lockouts (loud, recoverable); round 4 introduces a **fail-open** silent success — which this chain has consistently rated as the more severe class (P1-1, round 1, was exactly "uninstall reports `ok: true` while the bridge keeps running").

Two P2s and four P3s follow. The P1 fix is small and localised — I validated an 8-line illustrative patch that restores fail-closed on both interpreters while keeping all 68 candidate tests and all four of my independent experiments green.

---

## 1. R4-P1-A — `"absent"` conflates *deleted* with *unreadable* — **NEW, BLOCKING**

### The defect

Round 4 introduces the `"absent"` state in two places, each deciding it the same way:

`install_bridge.py:583` (in `_receipt_rows()`, used by `uninstall()` **and** `recover_pending_install()`):

```python
path = resolve_ssd_path(Path(raw_row["path"]), must_exist=False)   # :564
...
if not path.exists() and not path.is_symlink():                    # :583
    rows.append({**raw_row, ..., "state": "absent", "install_state": "absent"})
    continue
```

`install_bridge.py:1030` (in `verify()`, an independent copy of the same logic):

```python
path = resolve_ssd_path(Path(row["path"]), must_exist=False)       # :1029
if not path.exists() and not path.is_symlink():                    # :1030
    unreachable.append(os.fspath(path))
    continue
```

The code's own comment at `:564-567` asserts the opposite of what this implements:

> `must_exist=False`: a row whose target no longer exists at all (**an account genuinely deleted, not just temporarily undiscovered** …)

and at `:583-588`:

> Genuinely absent: **not "drift"** (nothing to compare against — there is no content at all), not a modeling gap.

`Path.exists()` cannot support that claim. It reports "does not exist" for *any* `stat()` failure it decides to ignore — not just `ENOENT`. Concretely, a managed config whose **parent directory is not traversable** still exists, still contains our live handler, and is still executed by Codex the moment the directory becomes readable again — but this check calls it absent, and every caller then treats it as "nothing to do".

Round 3 used `must_exist=True` here, so `resolve_ssd_path()`'s `path.resolve(strict=True)` raised `OSError` → clean `InstallError`. Round 4's `must_exist=False` removes that guard and replaces it with a test that silently swallows the distinction.

### Interpreter matrix (measured, not inferred)

`Path.exists()` handling of `EACCES` changed in CPython. Measured on this machine:

| Interpreter | Path | `Path.exists()` on an `EACCES` path | Round-4 behaviour |
|---|---|---|---|
| Python 3.9.6 | `/usr/bin/python3` — **the interpreter the README documents for all five actions** | **raises `PermissionError`** | bare traceback escapes `main()` (**P2 severity**) |
| Python 3.11.15 | `~/.local/bin/python3.11` | raises `PermissionError` | bare traceback |
| Python 3.14.6 | `/opt/homebrew/bin/python3` — **what plain `python3` resolves to on this machine** | **returns `False`** | silent `ok: true` (**P1 severity**) |

`README.md:134-154` documents `/usr/bin/python3 install_bridge.py <action>`, so the *documented* path yields the P2 traceback. The P1 is reachable today, on this machine, by an operator typing `python3 install_bridge.py uninstall` instead of the absolute path — `/opt/homebrew/bin` precedes `/usr/bin` in `PATH`. I am rating the finding by its worst reachable manifestation.

### Reproduction — the P1 (Python 3.14, real `main()` CLI)

`exp_h_absent_conflation.py` / `exp_h2_cli.py`. 4 managed configs, install normally, then `chmod 0o000` on one account's `home/` directory (the `hooks.json` inside is **untouched and still contains the bridge handler**), then drive the real CLI:

```
Path.exists() on this interpreter: False          (file is still on disk!)
direct stat() raises: PermissionError

verify    -> ok      ok=True checked=3 unreachable=1
   !! a config that EXISTS is reported as merely 'unreachable' and verify passes
uninstall -> ok      ok=True restored=3 unreachable=1
   latest-receipt.json still exists: False        <-- the receipt is DELETED

== after restoring the directory permission ==
victim handler count = 2                          <-- BRIDGE STILL ACTIVE
victim bytes unchanged from installed state = True
the hook command's script still on disk = True    -> it still executes on every Codex prompt
latest-receipt.json present = False               <-- no tool knows about it any more
```

Through the shipped entry point, `uninstall` **exits 0** and prints `{"ok": true, "restored": [...]}`.

The aftermath is worse than a leak. With the receipt gone, `verify` reports `not installed`, `recover` reports `none` — and the next `install` rediscovers the config, finds no previous-receipt row for it, and captures its **already-bridged** content as the new pristine baseline:

```
== can ANY tool action still clean it up? ==
plan       -> ok ok=True
verify     -> InstallError not installed: no receipt found
recover    -> ok none
install    -> ok rows=4
   victim handler count after reinstall = 2
uninstall  -> ok restored=4

FINAL victim handler count = 2
FINAL byte+mode exact pristine = False
```

That is **P1-1** — round 1's headline finding — reproduced end to end through the new `"absent"` path: `uninstall` reports success, the bridge handler is still live, and the true pristine baseline is permanently lost.

### Reproduction — the P2 (Python 3.9, documented invocation)

Same fixture, real `main()`:

```
======== py3.9 CANDIDATE, action=verify ========
exit code   : 1
stdout      : (empty)
stderr      : <12 lines> last = PermissionError: [Errno 13] Permission denied: '.../home/hooks.json'
honours the documented {"ok": ...} JSON contract : False
escaped as a Python traceback on stderr          : True

======== py3.9 CANDIDATE, action=uninstall ========
exit code   : 1
stdout      : (empty)
escaped as a Python traceback on stderr          : True
```

This is precisely the class of bug that P2-1, P2-2, `resolve_ssd_path()`'s guarded `stat()` and the seven `_mode_bits()` call sites exist to eliminate: round 4 adds two new **unguarded** stat-ing call sites (`:583`, `:1030`) that bypass all of them.

### Round-3 contrast — it failed closed and fully recovered

Identical fixture, identical `chmod`, `870810d468`:

| | py3.9 | py3.14 |
|---|---|---|
| `verify` | `{"ok": false, "error": "path unavailable: …"}` | `{"ok": false, "error": "path unavailable: …"}` |
| `uninstall` | `{"ok": false, …}`, receipt **preserved** | `{"ok": false, …}`, receipt **preserved** |
| after the permission is restored | `verify` ok (4 checked), `install` ok, `uninstall` ok → **final handler count = 1** | same → **final handler count = 1** |

Round 3 refused, kept the receipt, and recovered completely once the transient condition cleared. Round 4 declares success and destroys the only record of the baseline. This is a strict regression in the safety direction, on an input round 3 already handled.

### Why the trigger is ordinary

The reproducer is a single `chmod` on a directory the invoking user owns. The general condition is "`stat()` on a managed config path fails with anything other than `ENOENT`", which also covers:

- an account directory whose mode was tightened by a permissions-repair pass or a `chmod -R` mistake;
- an account directory created under a different uid (e.g. once touched by `sudo`) → `EACCES`;
- `EIO` from the **external USB SSD this entire installer is built around** — a partially failing device returns `EIO`, not `ENOENT`, on `stat()`.

Note the one case that *is* safe: a fully unmounted SSD makes `SSD_ROOT.resolve(strict=True)` fail first, so `resolve_ssd_path()` raises cleanly. It is the *partial* failure that is dangerous.

### Validated illustrative fix

Read-only: I patched **only my scratchpad copy**, never the repository. Replacing both `:583` and `:1030` call sites with an explicit errno-discriminating helper:

```python
def _path_is_absent(path: Path) -> bool:
    # ENOENT (genuinely deleted) is the ONLY thing that may be treated as
    # "absent". Any other stat failure (EACCES from a parent without +x, EIO
    # from the external SSD) is "cannot determine" and must fail closed.
    try:
        path.lstat()
        return False
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise InstallError(f"cannot determine whether {path} exists") from exc
```

Results with that patch applied to the scratchpad copy:

| Check | Result |
|---|---|
| `verify`/`uninstall` on an `EACCES` config, py3.9 | `InstallError: cannot determine whether … exists` (fail closed) |
| `verify`/`uninstall` on an `EACCES` config, py3.14 | `InstallError: cannot determine whether … exists` (fail closed) |
| EXP-A (genuine permanent retirement, 5 actions + new account + byte/mode-exact uninstall) | **PASS** |
| EXP-B (temporary rename across an upgrade) | **PASS** |
| EXP-C (pruned prior backup dirs) | **PASS** |
| EXP-D (path-addressed mid-upgrade interruption) | **PASS** |
| Candidate's own full suite | **68/68 OK** |

So the P1 is closable without disturbing anything this round fixed. (`is_symlink()` also swallows errors and should get the same treatment; a dangling symlink is `ENOENT` on `lstat` of the *resolved* path, which the helper handles.)

---

## 2. R4-P2-A — the R3-P2-A fix's stated invariant is false for carried-forward rows — **NEW, non-blocking**

`_receipt_rows()` deliberately keeps `backup` **eager** while making `prev_backup`/`after_backup` lazy, justified at `:570-578` by:

> `backup` … is the only one of the three backup files every caller of this function actually needs

and in the dispatch note by "每次 install() 都会在当前事务自己的备份目录里重新拷贝一份". That re-copy claim is true only for **discovered** paths. A carried-forward row is copied into the new receipt *verbatim* (`install():947`), so its `backup` keeps pointing into an **older** transaction's backup directory indefinitely — the exact situation R3-P2-A was about.

`exp_g_adversarial.py g1`, on the candidate:

```
carried row backup dir : 20260817T153910.357140Z    (v1's dir)
live    row backup dir : 20260817T153910.369007Z    (v2's dir)
-> carried row's backup is in the OLD dir: True
pruned v1 backup dir …

plan      : ok(ok=True)
verify    : ok(chk=3,unr=1)
recover   : ok(none)
install   : InstallError: install failed and the durable recovery journal remains pending
uninstall : InstallError: path unavailable: …
```

The `install` failure is the important part. The live writes all **succeed**; the failure happens in the commit-finalising `recover_pending_install()` inside `install()`'s `try` block, because `_receipt_rows()` cannot resolve the carried row's `backup`. The `except` path then calls `recover_pending_install()` again, which fails identically, so the pending journal is **never cleared**. Disk state = fully installed; reported state = failed; and from then on `read_receipt()` refuses every action with "pending install journal must be recovered first", while `recover` itself keeps failing on the same missing file. Unlike R4-P1-A and R4-P2-B there is **no tool-level escape hatch**.

Rated P2 on the same basis the reviewer rated R3-P2-A last round: it needs a backup directory to be pruned, which no code path does and which this file documents as never happening. But its blast radius is larger than R3-P2-A's (a stuck journal blocking all five actions, versus one failing `uninstall`), so the mitigation should not be assumed equivalent. Fixing it properly means either re-copying the pristine backup into the current transaction's directory for carried rows too, or making `backup` lazy per-row and only demanding it for rows `uninstall()` will actually write.

---

## 3. R4-P2-B — an `"absent"` row whose path reappears with different content locks `uninstall` — **behaviour change, non-blocking**

`exp_g_adversarial.py g2`. Retire an account (row carried forward as absent), then re-provision at the **same path** with fresh pristine content:

```
plan      : ok(ok=True)
verify    : InstallError: file is not private: …
recover   : ok(none)
install   : InstallError: hook config no longer matches the last known installed state (run verify or uninstall first)
uninstall : InstallError: refusing uninstall because a config changed: …
surviving handler counts: [2, 2, 2]      <-- bridge active in all three survivors
```

That is the familiar lockout signature. Three things keep it out of P1 territory:

1. **There is a working escape hatch.** `exp_g2_followup.py g2escape`: moving the re-provisioned file aside makes the row `"absent"` again, `uninstall` then succeeds and all survivors return byte+mode exact pristine. Verified.
2. **Round 3 is equally locked** on the identical scenario (`exp_g2_followup.py g2cross` on `870810d468`: `install`/`verify`/`uninstall` all refuse), so this is not a round-4 regression. Round 1/2 handled it benignly only *because* of the P1-R2-1 bug that dropped the row.
3. Refusing on content drift is the file's **intended** behaviour (`test_uninstall_refuses_when_config_changed_since_install`); the installer genuinely cannot tell a legitimately-fresh file from a tampered one.

Real account directories on the target machine are UUIDs (`0b4cd443-6d50-…`, `b9f32a51-16e9-…`), so same-path re-provisioning is unlikely; the more plausible route is an account's `hooks.json` being deleted and regenerated across two installs. Worth a follow-up (the error text could name the recovery step), not a blocker.

---

## 4. P3 items

- **R4-P3-A — `verify()` returns `ok: true` having verified nothing.** With every managed config absent, `verify` reports `ok=True checked=0 unreachable=4` and `uninstall` reports `ok=True restored=[]` (`exp_g_adversarial.py g3`). Defensible, but `ok: true` with an empty `configs` list is a misleading signal for an automated caller.
- **R4-P3-B — `uninstall()`'s `restored` list overstates.** It is `[row for row in rows if row["state"] != "absent"]` (`:1112`, `:1119`), so rows already at `before`/`both` — never written — are reported as restored.
- **R4-P3-C — `unreachable` can be stale.** `exp_g2_followup.py g5fix`: when an absent path reappears immediately after the uninstall journal is written, the finalising recovery **correctly** reverts it (byte+mode exact pristine, verified), but the returned `unreachable` list — computed pre-journal — still names it. The disk outcome is right; the report is wrong.
- **R4-P3-D — `install()` needlessly requires the prior transaction's `after_backup` to exist.** `:877-882` loads `previous_row["after_backup"]` with `must_exist=True` purely to populate `prev_raw[path]`, whose bytes it has *already* read from disk as `raw` and just verified against `after_sha256` at `:864`. With that directory pruned, `install` refuses (`exp_g_adversarial.py g4`) for a file it did not need. Pre-existing since round 2, not introduced here.
- **R4-P3-E — `plan()` is silent about carried-forward/absent rows.** It reports only discovered configs, so an operator diagnosing a retired account learns nothing from the one action that still works.
- Knowingly-deferred **R3-P3-A** (`.shared-runtime` left at `0o755` by a superseded version) and **R3-P3-B** (`0o000` `.shared-runtime`) are unchanged; I confirmed both remain unreachable on the target machine, which has never run any version. R3-P3-B is mildly worsened by the same `.exists()` conflation at `:785`, but `_acquire_exclusive_lock()`'s `ensure_private_dir()` chain still fails closed there.

---

## 5. Independent verification of each claimed fix

### (a) R3-P1-A / P1-R3-1 — permanently retired account — **FIXED**

`exp_a_retired.py` + `exp_a2_lockout_matrix.py`. My scenario: 4 managed configs with distinct pristine bytes and distinct pristine modes; the **middle** pooled account is retired by `shutil.rmtree` of its entire account tree (no rename-back, no `finally`); every action then driven, a brand-new account added, and finally `uninstall`.

Fresh-rig 5-action matrix after the retirement:

| Action | `870810d468` (round 3) | `d80d14c517` (candidate) |
|---|---|---|
| `plan` | `ok=True` (misleading) | `ok=True` |
| `verify` | `InstallError: path unavailable: …` | `ok` — checked=3, unreachable=1 |
| `recover` | `ok(none)` — no-op | `ok(none)` |
| `install` | `InstallError: … cannot be safely carried forward … (run uninstall first)` | `ok` — rows=4, retired row carried forward |
| `uninstall` | `InstallError: path unavailable: …` | `ok` — restored=3, unreachable=1 |
| surviving handler counts after `uninstall` | **`[2, 2]`** — bridge active, no way out | **`[1, 1]`** — bridge removed |

Full end-to-end on the candidate: brand-new account installs correctly while the retired row stays carried forward (rows=5); `uninstall` reports the retired path as `unreachable`; and every surviving config returns **byte-exact and mode-exact** to its own distinct pristine value (`0o644→0o644`, `0o600→0o600`, `0o604→0o604`); post-uninstall `verify` cleanly reports `not installed`.

**Temporary-rename half (original P1-R2-1) — also correct, and I made it stricter than the candidate's test** by performing an **upgrade** while the path was undiscovered, so the carried row's `after_*` belongs to a different release from every other row (`exp_b_temp_rename.py`):

- carried row keeps v1's `after_sha256` **and** the true original `before_sha256`;
- when the path reappears, `verify` puts it in `configs` (**not** `unreachable`) and matches it against **its own** carried baseline — its live owned command is still the v1 release;
- a further install re-adopts it with `before_*` = original pristine and `prev_*` = the v1 installed content it actually had;
- `uninstall` restores all 4 configs byte+mode exact.

Non-vacuity: on `870810d468` the same script fails at the install (the R3-P1-A refusal); on `09459cf6a0` it fails at `assert len(carried) == 1` because the row is silently dropped — the original P1-R2-1.

### (b) R3-P2-A — pruned prior-transaction backup directory — **FIXED**

`exp_c_pruned_backups.py`, harsher than the candidate's test: 4 configs, **three** installs (v1→v2→v3), then **both** v1's and v2's entire backup directories deleted (the candidate deletes one).

```
latest receipt row0:  backup -> v3 dir   prev_backup -> v2 dir   after_backup -> v3 dir
pruned v1 dir, pruned v2 dir   (prev_backup now gone: True)

verify    -> ok (checked=4)
uninstall -> ok (restored=4)
   all four configs byte_exact=True, mode exact=True  (0o644/0o600/0o640/0o604)
```

Non-vacuity: on `870810d468` the identical script fails — `uninstall -> InstallError: path unavailable: …/<v2 install_id>/<digest>-after.json` — the pruned prior transaction's after-backup, exactly R3-P2-A.

Boundary probe: with the *needed* `prev_backup` pruned and a mid-install failure injected, the candidate still produces a clean `InstallError`, never a bare exception. (The residual gap for *carried-forward* rows is R4-P2-A above.)

### (c) Interrupted-upgrade production behaviour — **FIXED**

`exp_d_interrupt.py`, in two interruption kinds and harder than the candidate's test in three ways: 4 managed configs (so the mixed state is multi-config), and a **carried-forward `"absent"` row present inside the very journal being rolled back** — the round-4 concept exercised through the round-2 recovery path.

**In-process injection** (path-addressed at the last live config's write):

```
receipt rows=4 (1 absent/carried) ; live configs=3
install -> InstallError: install failed; prior hook configs were restored
OBSERVED MIXED STATE at injection: 2 already-v2, 1 still-v1   (pending journal durable: True)

local-homes   ==PREV(v1):True  ==pristine:False  cmd_is_v1=True
7f3c1a02-aaa  ==PREV(v1):True  ==pristine:False  cmd_is_v1=True
c0de99e4-ccc  ==PREV(v1):True  ==pristine:False  cmd_is_v1=True

verify ok (chk=3 unr=1) | plan ok | recover state=none | uninstall ok
final: all three byte+mode EXACT pristine
```

**Real SIGKILL** at the same semantic point (child `returncode == -9` asserted), so no in-process handler can run:

```
ON-DISK MIXED STATE after the kill: 2 already-v2, 1 still-v1
fresh-process recover -> ok: state=rolled_back
… identical PREV-exact / all-actions-work / pristine-on-uninstall results as above
```

Both confirm the claim precisely: rollback lands on **PREV** (this transaction's start), not over-rolled to pristine and not left mixed; no extra manual `recover` is needed; `recover` afterwards is a clean `none`.

Non-vacuity: the same in-process script on `09459cf6a0` leaves 3 of 4 configs stuck at v2 with `verify → "pending install journal must be recovered first"` and `recover`/`uninstall → "pending install cannot roll back because a config drifted"` — R2-P1-A in full.

### (d) P2-R3-COMPAT — receipt schema v1→v2 — **FIXED**

`exp_e_schema.py`. I generated **real** receipts by running real installs with the older production code, then pointed the candidate at them.

| Receipt shape | Against | `verify` / `install` / `uninstall` error |
|---|---|---|
| real `870810d468` install (v1, **has** `prev_*`/`after_backup`) | candidate | `invalid receipt` (top-level) |
| real `09459cf6a0` install (v1, **lacks** `prev_*`/`after_backup`) | **round 3** | `invalid receipt config row` ← the complaint, reproduced |
| real `09459cf6a0` install (v1, lacks `prev_*`) | candidate | `invalid receipt` (top-level) |
| **v2** receipt with `configs[0].prev_backup` deleted (genuine corruption) | candidate | `invalid receipt config row` |

So old-schema rejection is now unambiguous *and* still distinguishable from real corruption. Confirmed independently that `RECEIPT_SCHEMA` is `…v1` at `29bb896012`/`09459cf6a0`/`870810d468` and `…v2` only at `d80d14c517`, and that the target machine has no live receipt to migrate (`.shared-runtime` does not exist).

---

## 6. (e) Test suite — 68/68 real, and the non-vacuity audit

`cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v` → **`Ran 68 tests … OK`**, genuinely green, run both in the repo worktree and in the pinned export (identical). Method count is 29 for `install_bridge` (9 `InstallBridgeTests` + 19 `InstallEndToEndTests` + 1 `UninstallCrashRecoveryTests`) + 39 hook tests = 68, matching the claim.

**Independent non-vacuity audit** — I ran the candidate's *test file* against the round-3 *production* code:

```
test_install_carries_forward_a_temporarily_undiscovered_config_without_losing_baseline ... ERROR
test_permanently_retired_account_does_not_lock_out_other_accounts                      ... ERROR
test_uninstall_survives_a_pruned_prior_install_backup_directory                        ... ERROR
(26 others ok)   ->  FAILED (errors=3)
```

Exactly the three the dispatch claims were baseline-validated. No spinning wheels among them.

**The interrupted-upgrade test specifically** (the one flagged as previously vacuous). Its assertions are now genuinely load-bearing:

- it captures state **inside** the injected failure callback and asserts `observed["pending_durable"] is True` — the journal really is on disk at that moment;
- it asserts `observed["main_command_mid_transaction"] != v1_command` — the first config really has already been rewritten to v2;
- `assertIn("pending_durable", observed, "the injected failure never fired")` guards against the injection silently not firing, which is what made the old call-count version vacuous.

So it is path-and-phase addressed as claimed, and it does produce observable mixed-state assertions. It passes on `870810d468` — correctly disclosed, since the underlying R2-P1-A fix predates it — but it is **not** toothless: run against `09459cf6a0` it **FAILS** with the rollback landing on the wrong release's command. My own EXP-D reaches a stronger 2-new/1-old mixed state with an absent row in the journal and confirms the same production behaviour.

**Coverage gap the suite cannot close as written**: every test runs under `/usr/bin/python3` (3.9) and no test ever creates a path that fails `stat()` with anything other than `ENOENT`. R4-P1-A is therefore invisible to this suite in both of its manifestations. A regression test needs (i) a `chmod 0o000` parent directory and (ii) an assertion that `uninstall` refuses rather than reporting `ok: true`.

---

## 7. (f) Regression sweep of rounds 1–3 — no regressions

`exp_f_regressions.py`, 24 checks with my own vectors — **23 pass, 1 false alarm from my own grep** (it flagged line 150, which is `_mode_bits()`'s own guarded body, and line 144, a comment). Manual audit of every stat-ing call site confirms all are guarded:

| Line | Call | Guard |
|---|---|---|
| 135 | `resolved.stat()` / `root.stat()` | inside `try/except OSError` (round-1 fix) |
| 150 | `stat.S_IMODE(path.stat())` | `_mode_bits()`'s own `try/except OSError` |
| 158, 168, 180 | `path.lstat()`, `os.fstat()` ×2 | `validate_owned_file()`'s `try/except OSError` |
| 198 | `path.lstat()` | `ensure_private_dir()`'s `try/except OSError` |
| 233 | `account_dir.lstat()` | `discover_hook_configs()`'s `try/except OSError: continue` |

All **seven** `_mode_bits()` call sites (613, 738, 756, 846, 960, 1094, 1137) go through the guarded wrapper; no raw `S_IMODE(path.stat())` remains outside it. (The unguarded `.exists()` at 583/1030 is R4-P1-A, reported separately.)

| Round 1–3 fix | Check | Result |
|---|---|---|
| `owned_handler` exact matching + P3-1 `comments=True` | 8 must-not-match vectors (prefix flag, prefix value, `#` comment with and without space, wrong adjacent flag, single-quoted whole pair) + 3 must-match | **0 false positives, 0 false negatives** |
| 5→7 `stat` guards | `_mode_bits` on a missing path | `InstallError` |
| P2-1 `atomic_write` bare `OSError` | write into a `0o500` directory | `InstallError` |
| P2-2 / R2-P2-B receipt shape | delete each of `release_id`, `release_dir`, `script_sha256`, `volume_uuid`, `policy_sha256` | all 5 → clean `invalid receipt`, no bare `KeyError` |
| P1-1 baseline poisoning | 4 installs incl. 2 upgrades, then `uninstall` | byte+mode exact pristine on all 4 configs |
| R2-P1-A `prev_*`/`before_*` separation | EXP-D, both interruption kinds | PREV-exact, verified above |
| R2-P1-B fresh-machine lock dir | real `main()` install with no pre-existing `.shared-runtime` | exit 0; chain is `0o700`/`0o700`; real `main()` uninstall exit 0 |
| P2-3 concurrency lock | second `_acquire_exclusive_lock()` | refused; re-acquirable after release; lock file `0o600` |
| P2-4 receipt cleared by uninstall | `latest-receipt.json` before/after | removed; `verify` → `not installed` |
| **P1-2 uninstall journal — new round-4 interaction** | real **SIGKILL** mid-`uninstall` **while the receipt holds an `"absent"` row** | child `-9`; journal survived; genuine mixed disk state (`handler counts=[1,2,2]`); fresh-process `recover` → `uninstalled`; all survivors byte+mode exact pristine; `verify` → `not installed` |

That last row is a scenario the candidate suite does not cover, and the candidate handles it correctly.

---

## 8. Recommendation

**NO-GO** on `d80d14c517`. Do not install, merge or activate.

Blocking:

1. **R4-P1-A** — replace the `Path.exists()`/`is_symlink()` absent-checks at `install_bridge.py:583` and `:1030` with an errno-discriminating check so that only `ENOENT` means `"absent"` and every other `stat()` failure raises `InstallError`. The validated illustrative patch is in §1; it keeps all 68 tests and all four of my experiments green. Add a regression test with a `chmod 0o000` parent asserting `uninstall` **refuses** — and, given the interpreter split, consider running the suite under both `/usr/bin/python3` and the `PATH` `python3`, or pinning the interpreter in the README/wrapper so the documented and actual invocations cannot diverge.

Should be fixed in the same pass:

2. **R4-P2-A** — a carried-forward row's `backup` is never re-copied, so pruning an old backup directory can wedge the pending journal with no tool-level recovery. Either re-copy the pristine backup for carried rows or make `backup` lazy per-row.
3. **R4-P2-B** — at minimum, make the drift refusal name the recovery step for a re-provisioned account (the escape hatch exists but is not discoverable from the error text).

Given that a new defect has now been introduced in five consecutive rounds — and that this round's is the first to fail *open* — I would also suggest the next round add, as a standing invariant test rather than a per-finding test, an assertion that **no action ever returns `ok: true` while any receipt-managed config still contains an owned handler**. Every P1 in this chain (P1-1, P1-R2-1, R3-P1-A, R4-P1-A) violates exactly that one property, and it is checkable directly.
