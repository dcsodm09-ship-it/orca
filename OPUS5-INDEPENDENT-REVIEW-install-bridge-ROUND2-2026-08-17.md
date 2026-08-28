# Independent read-only review — `install_bridge.py` round 2

- **Reviewer**: Claude opus5 / max effort, independent worker (`task_fd57aa2562f9`)
- **Candidate**: `09459cf6a0` — `claude-codex-memory-bridge/install_bridge.py` + `tests/test_install_bridge.py`
- **Round-1 baseline for the delta**: `29bb896012`
- **Date**: 2026-08-17
- **Mode**: read-only. Nothing was modified, committed, installed or activated. Every reproduction ran in throwaway `tempfile` sandboxes with a fake SSD root and monkeypatched module constants, in separate child processes. **No `install` was ever run against the real `/Volumes/Extreme SSD` Codex hook configs.** The only real-filesystem reads were `ls -ld` on `local-homes` and `.shared-runtime`.

---

## Verdict: **NO-GO**

Both round-1 P1s are genuinely and thoroughly fixed. I verified them independently, with my own fixtures, and they hold up under harder scenarios than the candidate's own tests use.

But **this round introduces two new P1s of its own**, both reproducible with no crash, no race and no privilege, and both regressions against `29bb896012`:

| ID | Introduced by | Effect | Repro needs |
|----|---------------|--------|-------------|
| **R2-P1-A** | fix ① (P1-1 baseline inheritance) | A failed or interrupted **upgrade** install is permanently unrecoverable — `recover`/`verify`/`install`/`uninstall` all refuse forever, bridge stays active, `plan` still reports `ok: true` | an ordinary I/O error on one config write. No crash required. |
| **R2-P1-B** | fix ⑤ (P2-3 flock) | The **very first `install` on a machine with no `.shared-runtime` yet` fails, permanently and not self-healingly | nothing — it is the default state of the real target SSD right now |

R2-P1-B in particular blocks the exact operation this whole review chain exists to authorise: `/Volumes/Extreme SSD/Orca/local-homes/.shared-runtime` **does not exist on this machine today** (verified), so the first real `install` hits it.

Neither P1 was mentioned in the commit message or the candidate's tests.

---

## 1. Round-1 P1s — independently confirmed fixed

### ① P1-1 (reinstall poisoning the uninstall baseline) — **FIXED**

I built my own sandbox, deliberately unlike the suite's fixture: **4** hook configs instead of 2, different account names (`zz-account`, `aa-account`, `mid-account`, sorted differently from insertion order), **four distinct original modes** (`0o644`, `0o600`, `0o640`, `0o604`), two pre-existing unrelated handlers per config plus a second event key, non-ASCII content, and 4-space JSON indentation (so a canonical-JSON rewrite is byte-detectable).

`exp_a_p1_1.py` — 16/17 checks pass (the 1 failure is a message-quality issue, filed as R2-P2-C below):

| Scenario | Result |
|---|---|
| A1 `install` ×3 (same release) → `uninstall` | byte-exact + mode-exact restore on all 4 configs; 3rd receipt's `before_sha256`/`before_mode` equal the true pristine originals; no `BRIDGE_ID` residue anywhere; `latest-receipt.json` cleared |
| A2 `install(v1)` → upgrade → `install(v2)` → upgrade → `install(v3)` → `uninstall` | 3 distinct `release_id`s; exactly 1 owned handler per config (no accumulation); `verify()` green; byte+mode exact restore |
| A3 two full install/uninstall cycles with an upgrade between them | exact both times |
| A4 a **new Codex account appears between install #1 and #2** | install #2 picks it up (5 rows); uninstall restores all 5 exactly, including the late account whose genuinely-pristine baseline came from current disk |

The inheritance logic is correct: a path already covered by a previous receipt inherits that receipt's `before_sha256`/`before_mode`/`backup`; a path with no previous row uses current disk content. A4 shows both branches working in the same transaction.

### ② P1-2 (uninstall had no transaction journal) — **FIXED**

`exp_b_p1_2.py` — **50/50 checks pass**. I used real `os.kill(SIGKILL)` in real child processes at **7 distinct interrupt points**, driving recovery from a genuinely fresh process each time:

| Kill point | On-disk state after kill | `recover` | Outcome |
|---|---|---|---|
| before the journal is written | 0/4 reverted, no journal | `none` | still cleanly installed; a normal uninstall afterwards works |
| after journal, 0/4 reverted | journal + latest present | `uninstalled` | fully clean |
| after journal, **1/4 reverted** | genuine mixed state | `uninstalled` | fully clean |
| after journal, **2/4 reverted** | genuine mixed state | `uninstalled` | fully clean |
| after journal, **3/4 reverted** | genuine mixed state | `uninstalled` | fully clean |
| after all reverts, before `latest-receipt.json` removal | 4/4 reverted, latest still present | `uninstalled` | fully clean |
| after `latest-receipt.json` removal, before journal removal | 4/4 reverted, latest gone | `uninstalled` | fully clean |

For every non-trivial point I confirmed: journal tagged `kind: "uninstall"`; all 4 configs restored **byte-exact and mode-exact**; no `BRIDGE_ID` residue; **`latest-receipt.json` cleared**; pending journal cleared; `verify()` reports `not installed: no receipt found`; and a subsequent `install` → `uninstall` cycle still lands byte-exact.

I also checked the case the round-1 report called out as the dangerous one (B8): killed mid-uninstall, then the operator runs `install` instead of `recover`. `install()`'s internal `recover_pending_install()` finishes the uninstall first, then installs from a genuinely pristine baseline, and the following `uninstall` restores true pristine. Correct.

The forward-only commit direction is right: an interrupted uninstall always finishes uninstalling and never reverts toward "still installed".

---

## 2. New P1s introduced by this round

### R2-P1-A — a failed/interrupted **upgrade** install is permanently unrecoverable

**Severity: P1.** Reproducible with no crash, no kill, no race, no privilege. Regression vs `29bb896012`.

**Root cause.** Fix ① redefined what a receipt row's `before_*` means:

- **before this round**: "the state this config was in when *this transaction* started"
- **after this round**: "the true pristine pre-bridge state"

These are the same thing for a first install, so the change looks safe. They diverge on a **re-install**. But `recover_pending_install()`'s *install-journal rollback* path still relies on the old meaning. `_receipt_rows()` (install_bridge.py:518-527) classifies each row by comparing current content against `before_*` and `after_*` only:

```python
before_matches = current_sha == raw_row["before_sha256"] and current_mode == raw_row["before_mode"]
after_matches  = current_sha == raw_row["after_sha256"]  and current_mode == raw_row["after_mode"]
...
else: state = "drift"
```

During an upgrade, a config that has **not yet been rewritten** still holds the *v1-installed* bytes. That matches neither `before_` (now pristine) nor `after_` (v2) — so it is classified `"drift"`, and install_bridge.py:587-589 hard-refuses:

```python
drifted = [row for row in rows if row["state"] == "drift"]
if drifted:
    raise InstallError("pending install cannot roll back because a config drifted")
```

Nothing has actually drifted. The rows are sitting in exactly the state the installer itself left them in.

**Reproduction 1 — real SIGKILL** (`exp_p1_lockout.py` R1). Install v1, upgrade the source, then SIGKILL install #2 on the first of four config writes. Disk is left at the *fully consistent v1 install*. Then:

```
recover    REFUSED  {"ok": false, "error": "pending install cannot roll back because a config drifted"}
verify     REFUSED  {"ok": false, "error": "pending install journal must be recovered first"}
install    REFUSED  {"ok": false, "error": "pending install cannot roll back because a config drifted"}
uninstall  REFUSED  {"ok": false, "error": "pending install cannot roll back because a config drifted"}
plan       SUCCESS  RC:0          <-- the only action that works, and it reports ok:true
```

The bridge remains active in all 4 configs the whole time.

**Reproduction 2 — no crash at all** (R2). Replace the SIGKILL with an ordinary `InstallError` from one config write — i.e. a transient disk error, a config that changed under us, a failed write verification. `install()`'s own `except BaseException` handler calls `recover_pending_install()`, which raises, so install reports:

```
install failed and the durable recovery journal remains pending
```

…and the machine lands in the identical permanent lockout. **No crash, kill, race or privilege is required to reach this.**

**Regression proof** (R3). The identical SIGKILL scenario against `29bb896012`:

```
baseline recover -> {"install_id": "...", "ok": true, "state": "rolled_back"}  RC:0
```

The baseline rolls back cleanly to v1 and stays usable, because there `before_*` still meant "state at transaction start". The lockout is new.

**Scope** (R4). Upgrade-specific. A same-release idempotent re-install is unaffected (its `after_sha256` equals the current content, so rows classify as `"after"` and roll back correctly). Upgrades — a new `claude_memory_hook.py` producing a new `release_id` — are precisely the operation fix ① was written to make safe.

**Escape hatches** (`exp_h_final.py` H1). Only one works: hand-deleting `pending-install.json` from the private runtime directory, after which `uninstall` succeeds and restores true pristine. No tool action can do it; the error text does not hint at it; reverting the source script to restore the old `release_id` does **not** help (the journal still blocks). An operator following the error messages has no way out.

**Secondary consequence of the same conflation.** When the interruption lands *after* all config writes but before `latest-receipt.json` (kill point 13 of 13), rollback does succeed — but it rolls back to **pristine**, silently uninstalling the previously-working v1 bridge rather than restoring it, leaving a stale `latest-receipt.json` and a confusing `file is not private: ...` error from `verify`. Recoverable (`uninstall` works), but the semantics are surprising and undocumented.

**Suggested direction** (not applied — read-only review): a receipt row needs to carry both facts, e.g. keep `before_*` as the true pristine baseline for uninstall, and add a separate `prev_sha256`/`prev_mode` recording the state at transaction start for the install journal's rollback to target. Alternatively, make the install-journal rollback treat "matches the *previous* receipt's `after_*`" as a valid, non-drift resting state.

### R2-P1-B — the first `install` on a machine with no `.shared-runtime` fails permanently

**Severity: P1.** Regression vs `29bb896012`. Hits the real target machine on the first real install.

**Root cause.** Fix ⑤ moved lock acquisition into `main()`, so `_acquire_exclusive_lock()` now runs **before** `install()`:

```python
lock_path = RUNTIME_BASE / "installer.lock"
lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
```

`pathlib.Path.mkdir(parents=True)` creates *missing parent* directories with the default `0o777 & ~umask`, **ignoring the `mode` argument** — CPython recurses with `self.parent.mkdir(parents=True, exist_ok=True)`, dropping `mode`. So `RUNTIME_BASE` itself gets `0o700`, but its parent `.shared-runtime` gets **`0o755`** under the standard umask 022. Confirmed directly (`exp_new_lock.py` N1: `requested mode=0o700 with parents=True -> outer=0o755 inner=0o700`).

`write_runtime()` then calls `ensure_private_dir(RUNTIME_BASE.parent)`, which rejects any directory with `mode & 0o077`:

```
{"ok": false, "error": "unsafe runtime directory: .../Orca/local-homes/.shared-runtime"}
```

**Not self-healing** (N3). Attempts 2 and 3 fail identically — the directory now exists at `0o755` and nothing ever chmods it. The only fix is a manual `chmod 700` (N5 confirms install then succeeds). N4 confirms causality: the same sandbox with `.shared-runtime` pre-created at `0o700` installs fine.

**This is the current state of the real target.** Verified read-only:

```
$ ls -ld "/Volumes/Extreme SSD/Orca/local-homes"
drwxr-xr-x@ 21 www1adwawd  staff  672 ... /Volumes/Extreme SSD/Orca/local-homes
$ ls -ld "/Volumes/Extreme SSD/Orca/local-homes/.shared-runtime"
ls: ...: No such file or directory
$ umask
022
```

**Regression proof** (`exp_d_baseline.py` D1), same fresh-machine fixture both sides:

```
BASELINE 29bb896012    RC:0  SRMODE:0o700
CANDIDATE 09459cf6a0   RC:1  SRMODE:0o755   {"ok": false, "error": "unsafe runtime directory: ..."}
```

The baseline creates `.shared-runtime` correctly because only `ensure_private_dir()` ever created it, and that uses an explicit `mkdir(mode=0o700, exist_ok=False)` after resolving an existing parent — it never relies on `parents=True`.

**Pre-flight is misleading** (N6). `plan` takes no lock, succeeds, creates nothing, and reports a healthy ready-to-go system — then `install` refuses. On a fresh machine the operator's dry run is green and the real run is not.

---

## 3. New P2/P3 issues

### R2-P2-A — `_acquire_exclusive_lock()` reintroduces the exact bug class fix ③ removed

`mkdir()` and `os.open()` in `_acquire_exclusive_lock()` (install_bridge.py:905-906) are **outside** the `try`, so a bare `OSError` escapes `main()`'s `except InstallError` as a raw traceback — the very failure mode fix ③ (P2-1) was added to eliminate, reintroduced in the same commit:

- N7: `.shared-runtime` unwritable → `install` ends in `PermissionError: [Errno 13] ...` traceback
- N8: `RUNTIME_BASE` exists as a regular file → `recover` ends in `FileExistsError: [Errno 17] ...` traceback

Both should be `{"ok": false, "error": ...}`. Also worth noting: `os.open()` here has no `O_NOFOLLOW`, unlike every other open in this file.

### R2-P2-B — fix ④ misses `release_id`, so `verify()` still raises a bare `KeyError`

`_validate_receipt_shape()` validates 10 of the 11 receipt fields, but not `release_id` — and `verify()` reads it unguarded at install_bridge.py:795. `exp_f_gap.py` F2 enumerates coverage:

```
bridge_id  validated    command      NOT VALIDATED
install_id validated    installed_at NOT VALIDATED
policy_sha256 validated release_id   NOT VALIDATED   <-- read as receipt["release_id"] at :795
release_dir validated   schema       validated
script_sha256 validated volume_uuid  validated
```

A receipt missing only `release_id` passes validation, passes every digest check, then:

```
verify(): BARE KeyError: 'release_id'
  File ".../install_bridge.py", line 795, in verify
CLI `verify` -> UNCAUGHT:KeyError        # escapes main()'s except InstallError
```

`uninstall` and `install` are unaffected, and `recover` handles it (F3). Narrow — needs a hand-corrupted-but-otherwise-consistent receipt — so **P3-level, not blocking**, but it means fix ④'s stated goal ("no bare KeyError/TypeError out of `verify()`/`uninstall()`") is not quite met.

### R2-P2-C — a drifted config is now a complete dead-end, with misleading advice

Side effect of fix ①. `exp_a_p1_1.py` A5: install, then make an ordinary user edit to your own `hooks.json` (add an unrelated hook):

```
verify     -> InstallError: hook config drift: ...
uninstall  -> InstallError: refusing uninstall because a config changed: ...
install    -> InstallError: hook config no longer matches the last known installed state
                            (run verify or uninstall first): ...
plan       -> ok
```

The refusal itself is defensible and the user's edit is correctly left untouched. Two problems:

1. **The advice is wrong.** "run verify or uninstall first" names two actions that *also* fail. Before this round, `install` was the one action that made progress (via the P1-1 bug); now nothing does. Recovery requires reconstructing the file to byte-match a `canonical_json` digest by hand.
2. Only `plan` works, and it reports `ok: true` with `will_change: true` — implying an install that will in fact be refused.

Grading this **P2**: the state is loud (clear errors, no silent wrongness, no data loss) but there is no documented recovery path, and the message actively misdirects. If anything other than a human ever rewrites `hooks.json` — another installer, a Codex-side migration, a formatter — this becomes reachable without user error.

### R2-P3 items (minor, non-blocking)

- **Test does not demonstrate its stated fix.** `test_corrupted_receipt_row_raises_install_error_not_a_bare_exception` **passes against `29bb896012` too** (`exp_d_baseline.py` D2). It deletes `configs[0]["path"]`, which `verify()` already caught inline at install_bridge.py:781-782 before this round. It is a valid assertion but it does not exercise the P2-2 fix. 11 of the 13 corruption vectors I tried do; the test picked one of the 2 that were already covered. Same category as the comment-vector test opus flagged in round 1.
- **Dead code**: `receipt_raw = canonical_json(receipt)` at install_bridge.py:823 in `uninstall()` is assigned and never used.
- **Unbounded backup growth**: one `backups/<install_id>/` directory per install, never pruned — 5 idempotent re-installs leave 5 backup dirs (`exp_g` H-series). Cosmetic.
- **Deleting a Codex account after install blocks `uninstall`** (`resolve_ssd_path(..., must_exist=True)` on a receipt row's path). Fails closed with a clean `InstallError`; pre-existing, not a round-2 regression.

---

## 4. P2/P3 fixes ②–⑦ — verified working, no false positives

`exp_c_p2p3.py` — 74/76 (the 2 failures are R2-P2-B above and one over-crude assertion of mine, resolved in §5).

| Fix | Verification | Result |
|---|---|---|
| ③ P2-1 `atomic_write` OSError wrapping | 4 real failure modes: mkdir into a read-only parent, parent is a regular file, mkstemp in a read-only dir, over-long path component | all → `InstallError`, none bare |
| ④ P2-2 receipt validation | 13 corruption vectors across rows and top level | 11 → clean `InstallError` from all of verify/uninstall/install; `del command` benign; `del release_id` → R2-P2-B |
| ⑤ P2-3 lock | real cross-process contention with a live holder | `install`/`uninstall`/`recover` correctly blocked with `another install_bridge.py invocation is already running`; `verify`/`plan` correctly **not** blocked; lock auto-released when the holder dies — **no stale-lock false positive**; install still intact afterwards |
| ⑥ P2-4 receipt clearing | install → uninstall | `latest-receipt.json` present then cleared; both `verify` and `uninstall` return exactly `not installed: no receipt found`; release dir retained as documented |
| ⑦ P3-1 `comments=True` | 9 must-not-match + 5 must-match vectors, plus an end-to-end install | see below |

**`owned_handler` false-positive / false-negative sweep** — all 14 correct:

*Correctly NOT owned* (would otherwise be deleted by `update_hook_config`): shell comment with adjacent pair; comment with no space (`#--bridge-id`); comment after real args; `BRIDGE_ID` as a substring suffix or prefix; flag present but not adjacent; `--bridgeid`; the equals form; a mere log message mentioning the ID.

*Correctly owned*: plain adjacent pair; the real generated command; single-quoted value; double-quoted value; **a quoted `#` that is not a comment** (`/bin/echo '#' --bridge-id <ID>`) — confirming `comments=True` did not introduce a false negative on quoted content. The generated command is built with `shlex.quote`, so it can never contain an unquoted `#`.

End-to-end: a real user hook `/usr/bin/true # --bridge-id <ID>` placed in a live config **survives install**, exactly one genuinely-owned handler is added, and `uninstall` restores the file byte-exactly.

---

## 5. Regression check on round-1's already-accepted fixes — clean

- **5 stat guards**: only **one** real `stat.S_IMODE(...)` call site remains, inside `_mode_bits` (the second grep hit is prose inside `_mode_bits`'s own comment — my initial count assertion was too crude and is what produced the C7 "failure"). Both `resolve_ssd_path` stat calls are guarded. `_mode_bits` on a missing file → `InstallError`, not bare `OSError`.
- **`owned_handler` exact-token adjacency matching**: intact (`token == "--bridge-id"` + `tokens[index + 1] == BRIDGE_ID`), and the sweep above confirms behaviour.
- **Install-side transaction under real SIGKILL**: round 1 checked 4 interrupt points; I checked **all 13** `atomic_write` points of a 4-config first install. Every one lands cleanly on exactly one of *untouched-pristine* or *fully installed* — never a partial state — and each rollback is **byte-exact and mode-exact**. Points 1–8 leave no journal (`state: none`, pristine); points 9–13 roll back (`state: rolled_back`, pristine). No regression.

(The upgrade-install kill matrix is a different story — that is R2-P1-A, and it is new behaviour, not a regression of the round-1 fix.)

---

## 6. Test suite (task d)

```
$ cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v
Ran 63 tests in 0.403s
OK
```

**63/63 genuinely green**, run independently. Python 3.9.6.

**Non-vacuity check** — I ran the candidate's test file against the round-1 baseline module (`29bb896012`) directly:

| Test | Against baseline |
|---|---|
| `test_reinstall_does_not_poison_the_uninstall_baseline` | **FAILS** ✓ non-vacuous |
| `test_upgrade_reinstall_does_not_poison_the_uninstall_baseline` | **FAILS** ✓ non-vacuous |
| `test_sigkill_mid_uninstall_is_fully_completed_by_recovery` | **FAILS** ✓ non-vacuous |
| `test_verify_and_uninstall_report_not_installed_after_uninstall` | FAILS ✓ |
| `test_atomic_write_wraps_bare_oserror_in_install_error` | FAILS ✓ |
| `test_concurrent_installer_invocations_fail_the_lock_instead_of_interleaving` | FAILS ✓ |
| `test_owned_handler_rejects_bridge_id_as_a_mere_substring` | FAILS ✓ |
| `test_corrupted_receipt_row_raises_install_error_not_a_bare_exception` | **PASSES — vacuous for its stated purpose** (R2-P3) |

The three headline claims hold: those tests really do fail on the old code, and the SIGKILL test's methodology is sound (a real child process, a real `os.kill`, recovery driven from a genuinely fresh process).

**Coverage gaps the suite has, which is why both new P1s slipped through:**
- No test kills or fails an install that is an **upgrade** — every install-crash test starts from a clean machine (→ R2-P1-A).
- No test runs `main()` end to end, and every fixture pre-creates `.shared-runtime` via `local_homes.mkdir(parents=True)` before the installer ever touches it (→ R2-P1-B). The lock test calls `_acquire_exclusive_lock()` directly, after `setUp` already built the tree.

---

## 7. Deliberately deferred P3s (task f) — all confirmed still non-blocking

| Item | Assessment |
|---|---|
| **P3-2** `--bridge-id=<ID>` equals form | Fails in the **safe direction**: it is a false *negative*, so a third party's hook is never deleted. The installer only ever emits the space-separated form (verified: `"--bridge-id",` present, no `--bridge-id=` anywhere). Non-blocking. |
| **P3-3** whole-handler-group granularity | Confirmed reachable: a hand-merged group containing both a user hook and ours is classified owned and the whole group is dropped on install. But this installer never produces that shape, it requires a human to manually merge, and **`uninstall` restores the file byte-exactly** — the loss is transient, not permanent. Non-blocking. |
| **P3-4 / P3-5** TOCTOU windows | Pre-date round 1, unchanged this round. Non-blocking. |
| **P3-6** `update_hook_config(remove=True)` | Still zero production callers (`remove=True` appears 0 times outside the signature). Non-blocking. |
| **P3-7** `resolve_ssd_path(must_exist=False)` | Still zero production callers. Non-blocking. |

The deferral decisions were sound and honestly recorded in the commit message.

---

## 8. What must happen before round 3

Blocking:

1. **R2-P1-A** — separate "state at transaction start" from "true pristine baseline" in a receipt row, so the install journal can roll an interrupted upgrade back to the previous working install instead of classifying it as drift. Add a regression test that interrupts an **upgrade** install (both by SIGKILL and by an ordinary write failure) at each config-write point and asserts every tool action still makes progress.
2. **R2-P1-B** — do not rely on `mkdir(parents=True)` for a directory that must be private. Create `.shared-runtime` the way `ensure_private_dir()` does (explicit `mkdir(mode=0o700)` on an already-resolved parent), or acquire the lock after `write_runtime()` has established the tree, or re-`chmod` after creation. Add a test that runs `main("install")` end to end on a fixture where `.shared-runtime` does **not** pre-exist.

Non-blocking but worth folding into the same pass, since they are all in the code this round touched:

3. **R2-P2-A** — guard `mkdir`/`os.open` in `_acquire_exclusive_lock()`; consider `O_NOFOLLOW`.
4. **R2-P2-B** — add `release_id` to `_validate_receipt_shape()`.
5. **R2-P2-C** — give the drift dead-end an actual exit, and fix the misdirecting error text.
6. **R2-P3** — replace the vacuous corruption vector in `test_corrupted_receipt_row_...` with one the baseline does not already catch (e.g. a non-string `backup`, or a missing `before_mode`); drop the dead `receipt_raw` at install_bridge.py:823.

Positively: the two round-1 P1s are properly fixed, the uninstall journal design is sound, and the `owned_handler` and transaction work all hold up under adversarial probing. The problems here are narrow interaction bugs between the new fixes and existing invariants, not design failures.

---

## Appendix — reproduction scripts

All under the session scratchpad, runnable with `/usr/bin/python3` (they import the candidate module directly and build their own sandboxes):

| Script | Covers |
|---|---|
| `harness.py` | isolated fake-SSD sandbox, 4 configs, 4 distinct modes, child-process runner |
| `exp_a_p1_1.py` | (a) P1-1 reinstall/upgrade/late-account/dead-end |
| `exp_b_p1_2.py` | (b) P1-2, 7 real-SIGKILL interrupt points |
| `exp_c_p2p3.py` | (c)+(e) fixes ③–⑦, `owned_handler` sweep, 13-point install-kill matrix, stat guards |
| `exp_d_baseline.py` | (d) fresh-machine baseline diff + test non-vacuity |
| `exp_new_lock.py` | R2-P1-B, R2-P2-A |
| `exp_g_upgrade_kill.py` | upgrade-kill matrix that surfaced R2-P1-A |
| `exp_p1_lockout.py` | R2-P1-A minimal repro + regression proof + scope control |
| `exp_f_gap.py` | R2-P2-B receipt-field coverage |
| `exp_h_final.py` | (f) deferred-P3 confirmation + escape-hatch analysis |
