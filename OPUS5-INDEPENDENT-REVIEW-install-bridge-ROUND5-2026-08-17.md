# Independent read-only review — `install_bridge.py` round 5

**Reviewer:** Claude `opus` / effort `max`, independent read-only pass
**Candidate:** `9e2205dc43` (`fix(install_bridge): fail closed instead of fail open on an unreadable managed config`)
**Baseline:** `d80d14c517` (round-4 candidate)
**Scope:** `claude-codex-memory-bridge/install_bridge.py`, `claude-codex-memory-bridge/tests/test_install_bridge.py`
**Date:** 2026-08-17
**Nothing was modified, committed, installed, or activated.** Every reproduction ran in a throwaway
`tempfile.mkdtemp()` sandbox with `SSD_ROOT`/`LOCAL_HOMES_ROOT`/`RUNTIME_BASE`/`PENDING_PATH`/`SOURCE_SCRIPT`
rebound to that sandbox. The real `/Volumes/Extreme SSD/Orca/local-homes` was read for layout only; it still has
no `.shared-runtime`, i.e. this bridge has never been installed on the target machine.

---

## Verdict: **NO-GO**

Both round-4 findings are **genuinely and completely fixed**, and I confirmed each one independently on both
interpreters with my own scenarios (different account count, different directory level, different `chmod` value,
different install sequence). The regression sweep of rounds 1–4 is clean — 10/10 on my own checks, 70/70 on the
candidate suite, and both new tests are non-vacuous.

But the R4-P2-A fix — making `backup` lazily loaded in `_receipt_rows()` — silently removed a precondition that
`uninstall()` depended on. Round 4 validated every needed backup **before** writing the durable uninstall journal,
so an unloadable backup was a clean, nothing-happened refusal. Round 5 discovers the same problem **after** the
journal is durable and after earlier rows have already been reverted, producing a **half-uninstalled system that
no tool action — including `recover`, whose entire purpose is to clear exactly this state — can move forward**.

This is the fifth consecutive round in which the fix for the previous round's finding introduces a new, reproducible
defect of its own, and the second consecutive round in which the new defect is in the "every action refuses forever"
lockout family that made rounds 2 and 3 NO-GO.

| id | severity | status |
|---|---|---|
| **R5-P1-A** — lazy `backup` removed `uninstall()`'s pre-journal precondition → wedged journal + partial revert | **P1, blocking** | **NEW this round** |
| **R5-P2-A** — `install()` commits everything, then wedges its own journal on an unreadable carried-forward row | P2, non-blocking | NEW this round |
| R5-P3-A — `discover_hook_configs()` is the third same-class call site and was not fixed | P3 | pre-existing (= R3-P3-B), newly relevant |
| R5-P3-B — `backup` lost `resolve_ssd_path`'s same-device check | P3 | NEW this round |
| R5-P3-C — `ENOTDIR` flipped from "absent" to "indeterminate" | P3 | NEW this round |
| R4-P1-A | — | **FIXED** (verified independently, both interpreters) |
| R4-P2-A | — | **FIXED** (verified independently) |
| rounds 1–4 confirmed fixes | — | **no regressions** (10/10 independent checks) |

---

## 0. Environment and interpreter matrix

I re-measured the interpreter split the round-4 report turns on, because everything below depends on it.

```
$ /usr/bin/python3 -V   → Python 3.9.6     (the interpreter the docs/tests/`make_release()` command use)
$ python3 -V            → Python 3.14.6    (/opt/homebrew/bin/python3, first on PATH,
                                            and what `#!/usr/bin/env python3` on line 1 resolves to)
```

On a path whose parent directory is not searchable (`EACCES`):

| call | 3.9.6 | 3.14.6 |
|---|---|---|
| `Path.exists()` | **raises `PermissionError`** | **returns `False`** |
| `Path.is_symlink()` | **raises `PermissionError`** | **returns `False`** |
| `Path.is_file()` | **raises `PermissionError`** | **returns `False`** |
| `Path.lstat()` | raises `PermissionError` | raises `PermissionError` |

`scratchpad/r5/exists_probe.py`. This confirms the round-4 report exactly: the same input produced a bare traceback
on the documented interpreter and a silent fail-open on the one the shebang picks. `_path_is_absent()`'s use of
`lstat()` is the right primitive — it is the only one of the four that behaves identically on both.

All reproductions below were run on **both** interpreters unless noted; results were identical unless a difference
is called out.

---

## 1. R5-P1-A — the lazy-`backup` change removed `uninstall()`'s pre-journal precondition — **NEW, BLOCKING**

### The defect

Round 4's `_receipt_rows()` (`d80d14c517:576`) read and digest-checked **every** row's `backup` eagerly:

```python
backup = resolve_ssd_path(Path(raw_row["backup"]))          # must_exist=True
backup_raw = _load_backup(backup, raw_row["before_sha256"]) # reads + digest-checks now
```

Round 5 removes both (`install_bridge.py:609`) and defers the read to the two call sites that write from it:

```python
backup = resolve_ssd_path(Path(raw_row["backup"]), must_exist=False)   # :609
...
backup_raw = _load_backup(row["backup_obj"], row["before_sha256"])     # :1125, inside uninstall()'s loop
```

That is the correct cure for R4-P2-A. But `uninstall()` writes its durable journal at `:1116–1120`, i.e.
**before** the loop at `:1122`. The eager read was the only thing that guaranteed "if any backup this transaction
needs is unloadable, we find out before anything becomes durable." Removing it converts the failure from
*pre-transaction refusal* to *mid-transaction abort*, and `uninstall()`'s own `except BaseException` recovery
(`:1136`) cannot help — `recover_pending_install()`'s uninstall branch loads the identical backup at `:782` and
fails identically, so it re-raises as
`InstallError("uninstall failed and the durable recovery journal remains pending")` (`:1140`).

The journal then blocks every action: `read_receipt()` refuses (`:1014`), so `verify` is blocked; `install()`,
`uninstall()` and `recover` all begin by calling `recover_pending_install()`, which re-hits the same unloadable
backup. Only `plan` still returns, with `ok: false`.

### Reproduction — through the real `main()` CLI, minimal trigger

One install, **one** filesystem action, one uninstall. No crash, no race, no privilege, no concurrency, no
carried-forward rows, no multi-install sequence. `scratchpad/r5/exp_cli.py` + `scratchpad/r5/cli.py`
(argparse dispatch, `flock`, and the `except InstallError` → JSON boundary are all the real ones).

```
python=/usr/bin/python3  candidate=9e2205dc43
  $ install_bridge.py install   -> rc=0
  [operator frees space on the external SSD] rm -rf backups/20260817T161726.248200Z
  $ install_bridge.py uninstall -> rc=1 {"ok": false, "error": "uninstall failed and the durable recovery journal remains pending"}
  pending journal on disk: True
  configs still bridged: {'.codex': True, 'pooled-alpha': True, 'pooled-bravo': True}
  --- every remaining action ---
  $ install_bridge.py plan      -> rc=0 {"ok": false, "pending_transaction": true}
  $ install_bridge.py verify    -> rc=1 {"ok": false, "error": "pending install journal must be recovered first"}
  $ install_bridge.py recover   -> rc=1 {"ok": false, "error": "cannot read .../backups/2026.../0a2b454b….json"}
  $ install_bridge.py uninstall -> rc=1 {"ok": false, "error": "cannot read .../backups/2026.../0a2b454b….json"}
  $ install_bridge.py install   -> rc=1 {"ok": false, "error": "cannot read .../backups/2026.../0a2b454b….json"}
```

**The same input on the round-4 baseline `d80d14c517`:**

```
  $ install_bridge.py uninstall -> rc=1 {"ok": false, "error": "path unavailable: .../backups/2026.../88338d6e….json"}
  pending journal on disk: False          <-- nothing became durable
  --- every remaining action ---
  $ install_bridge.py plan      -> rc=0 {"ok": true,  "pending_transaction": false}
  $ install_bridge.py verify    -> rc=0 {"ok": true,  "unreachable": []}
  $ install_bridge.py recover   -> rc=0 {"ok": true,  "state": "none"}
```

Round 4 refuses cleanly and every other action keeps working. Round 5 wedges.

### The worse flavour — a *partially reverted* system

`scratchpad/r5/exp_b.py returns`, my own sequence (4 managed configs; an account parked by moving its **whole
account directory** out of `codex-accounts/`, not by renaming its `hooks.json`; three installs; the parked account
then comes back):

```
  row state=after    .codex/hooks.json
  row state=after    pooled-alpha/home/hooks.json
  row state=after    pooled-bravo/home/hooks.json
  row state=after    pooled-charlie/home/hooks.json      <-- backup -> pruned v1 dir
  uninstall: InstallError -> uninstall failed and the durable recovery journal remains pending
  after uninstall:  main   bridged=false 0o644     <-- reverted
                    alpha  bridged=false 0o644     <-- reverted
                    bravo  bridged=false 0o644     <-- reverted
                    charlie bridged=TRUE  0o600    <-- still executing the bridge on every Codex prompt
  plan    -> ok:false, pending_transaction:true
  verify  -> InstallError: pending install journal must be recovered first
  uninstall / recover / install -> InstallError: cannot read <pruned backup>
```

Round 4, same input: `InstallError("path unavailable: <backup>")`, **no journal, nothing reverted, all four configs
untouched**, and `plan`/`verify`/`recover` all still `ok: true`.

So round 5 turns a clean refusal into: three configs silently un-bridged, one still live, a durable journal that
says "an uninstall is in flight", and no tool action that can finish or abandon it.

### Recoverability

Two flavours, `scratchpad/r5/exp_c.py`:

- `chmod 0o000` on the backup directory (transient): wedged while the condition lasts; once permissions are
  restored, `recover` correctly returns `{"state": "uninstalled"}` and finishes. Recoverable, but every action is
  dead in the meantime.
- `rm -rf` the backup directory (permanent): **never** recoverable through the tool. Even manually deleting
  `pending-install.json` does not help — `uninstall` re-writes the journal and wedges again on the next attempt.

### Why the trigger is ordinary

Each ingredient is already modelled by this file's own design:

1. **A backup directory that is gone or unreadable.** `install()`'s comment at `:938–940` says old backup dirs "are
   never pruned" — *by the tool*. **R3-P2-A exists precisely because opus judged external pruning a real scenario,
   and round 3 shipped a fix for it**; round 5's own commit message cites that lineage. This is a bridge whose
   entire backup store lives on a removable external SSD; one `rm -rf` to free space, or one permission change
   during a hardening pass, is all it takes.
2. **A path whose row is not `absent`.** Any ordinary installed config qualifies. The minimal repro above uses the
   *current* transaction's own backup directory — no carried-forward row is involved at all.
3. Nothing else. No crash, no race, no privilege, no concurrency.

### Validated illustrative fix

Restore the precondition without undoing R4-P2-A — load only the backups the transaction will actually write from,
before the journal exists. `absent`/`before`/`both` rows are still never required, so a carried-forward row's pruned
backup remains harmless.

```python
    unreachable = [os.fspath(row["path_obj"]) for row in rows if row["state"] == "absent"]
    # Load every backup this transaction will actually write from BEFORE the
    # durable journal exists. _receipt_rows() used to validate all of them
    # eagerly, which is what made an unloadable backup a clean, nothing-
    # happened refusal; making `backup` lazy (R4-P2-A) removed that implicit
    # precondition and turned the same input into a half-reverted config set
    # plus a wedged journal no tool action could clear. Skipping
    # before/both/absent rows keeps the R4-P2-A fix intact -- a carried-
    # forward row's pruned backup is still never required.
    backup_bytes = {
        os.fspath(row["path_obj"]): _load_backup(row["backup_obj"], row["before_sha256"])
        for row in rows
        if row["state"] not in ("before", "both", "absent")
    }
    receipt_raw = canonical_json(receipt)
    atomic_write(PENDING_PATH, canonical_json({...}), 0o600)
    try:
        for row in rows:
            if row["state"] in ("before", "both", "absent"):
                continue
            backup_raw = backup_bytes[os.fspath(row["path_obj"])]
            atomic_write(row["path_obj"], backup_raw, row["before_mode"])
            ...
```

Applied to a scratchpad copy only (`scratchpad/r5/fixcheck/`, the repo was **not** touched), I measured:

- candidate suite: **70/70 OK**, unchanged;
- minimal repro: `uninstall` now returns `{"ok": false, "error": "cannot read <backup>"}`, **no journal**, nothing
  reverted, and `plan`/`verify`/`recover` all `ok: true` again — round-4's clean behaviour, without round-4's bugs;
- `exp_b.py returns`: no journal, no partial revert, tool fully usable;
- `exp_b.py carried` (R4-P2-A's scenario): still fixed — v3 install ok, no journal, `uninstall` ok with the parked
  account reported `unreachable`, surviving configs exactly pristine;
- `exp_a.py readonly` (R4-P1-A's scenario): still fixed.

`recover_pending_install()`'s uninstall branch (`:776–787`) has the identical shape and deserves the same treatment
in the same batch, though it is less urgent: by the time it runs the journal already exists, so it cannot *create*
the wedge — it can only fail to clear one.

---

## 2. R5-P2-A — `install()` writes everything, then wedges its own journal — **NEW, non-blocking**

`discover_hook_configs()` classifies a managed config with `candidate.is_file()` (`:239`), which — on 3.14 — silently
drops an `EACCES` account from discovery. The row is then carried forward (`:852`), and `install()`'s
commit-finalising `recover_pending_install()` at `:997` calls `_receipt_rows()` → `_path_is_absent()` → `InstallError`.
Both the retry at `:1002` and the original fail, so install ends in
`InstallError("install failed and the durable recovery journal remains pending")` — **after** every discovered config
and `latest-receipt.json` have already been written.

`scratchpad/r5/exp_a.py plan_install`, Python 3.14.6, one `chmod 0o600` on an account directory:

```
  install -> InstallError: install failed and the durable recovery journal remains pending
  pending journal: True
  snapshot: main bridged=true, pooled-alpha bridged=true, pooled-charlie bridged=true,
            pooled-bravo <unreadable>, latest_receipt=true
  plan    -> ok:false (pending)     verify -> blocked      uninstall -> blocked      recover -> blocked
  --- after restoring permissions ---
  recover/verify/uninstall -> all ok, full clean-up
```

The direction is right (refusing to declare success while a managed config is indeterminate beats round 4's silent
`ok`), and it self-heals once the permission is restored, so this is not blocking. Two things still deserve fixing
in the same batch:

- The operator is told **"install failed"** while the bridge is in fact live in every discovered config. The report
  and the disk disagree.
- The check happens only at commit. `install()` could classify carried-forward rows before it writes anything, and
  refuse (or proceed knowingly) instead of committing first and wedging second.

On 3.9 this is unreachable because `candidate.is_file()` raises first (see R5-P3-A).

---

## 3. P3 items

- **R5-P3-A — `discover_hook_configs()` is the third same-class call site and was not fixed.** This round's stated
  goal is "fail closed instead of fail open on an unreadable managed config." Two of the three places that classify
  an unreadable managed config were fixed; `candidate.is_file()` (`:239`) was not. On the documented
  `/usr/bin/python3` 3.9 it **raises a bare `PermissionError` out of `main()` with empty stdout** for `plan` and
  `install` (`exp_a.py plan_install`), breaking the "clean JSON on every failure" guarantee; on 3.14 it fails open
  and feeds R5-P2-A. Identical on `d80d14c517`, so **not a regression** — this is R3-P3-B, previously reported and
  knowingly deferred. It is worth re-raising only because the round's **own new test** creates exactly this
  filesystem state (`chmod 0o000` on an account home) and never exercises `plan`/`install` under it, so the suite
  reads as covering this condition when it covers only half of it.
- **R5-P3-B — `backup` lost the same-device check.** `resolve_ssd_path()` runs the `st_dev` comparison
  (`:124–139`) only when `must_exist=True`; this round changed `backup` to `must_exist=False` (`:609`). A `backup`
  under a *different* volume mounted inside the SSD tree is no longer rejected at row-build time, and
  `validate_owned_file()` checks uid/mode/`O_NOFOLLOW` but never `st_dev`. Low impact: `_load_backup()` still
  digest-checks the bytes against the receipt's `before_sha256`, so content substitution still fails, and
  `prev_backup`/`after_backup` have had `must_exist=False` since round 3 — this only equalises `backup` with them.
- **R5-P3-C — `ENOTDIR` flipped from "absent" to "indeterminate".** `pathlib`'s `_ignore_error` swallows `ENOTDIR`,
  so round 4 called it absent; `_path_is_absent()` now raises. Measured on both interpreters
  (`scratchpad/r5/enotdir.py`): a managed path whose parent directory has been replaced by a regular file — a path
  that provably *cannot* exist — now makes `verify`/`uninstall` refuse with the inaccurate message "cannot determine
  whether … exists". Same shape as R4-P2-B (opus rated that non-blocking because an escape hatch exists); the escape
  here is to remove the offending file, restoring `ENOENT`. Not blocking, but `ENOTDIR` arguably belongs with
  `FileNotFoundError` in the absent branch.
- **R4-P2-B and R4-P3-A…E confirmed unchanged**, as the candidate states. By code reading: `uninstall()`'s
  `restored` list is still `state != "absent"` (`:1146`, `:1153`), so rows already at `before`/`both` — never
  written — are still reported as restored (R4-P3-B); `unreachable` is still computed pre-journal (`:1114`)
  (R4-P3-C); `install()` still requires `previous_row["after_backup"]` to exist (`:906`) without reading its bytes
  for anything it has not already read (R4-P3-D); `plan()` still reports only discovered configs (`:1163`)
  (R4-P3-E). Deliberately deferred, and I agree none of them is blocking.
- Round 3's deferred **R3-P3-A**/**R3-P3-B** remain unreachable on the target machine, which still has no
  `.shared-runtime` directory (verified by listing `/Volumes/Extreme SSD/Orca/local-homes`).

---

## 4. (a) R4-P1-A — independently verified **FIXED**

My scenario, deliberately not the candidate test's: **4** managed configs (the test uses 2, then 3); the permission
tightened on the **account directory**, one level above the test's target (`<acct>/home`); mode **`0o600`**, not
`0o000`. `scratchpad/r5/exp_a.py readonly`.

```
--- baseline install (4 configs, all bridged) ---
--- chmod 0o600 pooled-bravo   (hooks.json itself untouched, still holds a live bridge handler) ---
  resolve_ssd_path(must_exist=False) -> ok        _path_is_absent -> InstallError
  verify    -> InstallError: cannot determine whether .../pooled-bravo/home/hooks.json exists
  uninstall -> InstallError: cannot determine whether .../pooled-bravo/home/hooks.json exists
  pending journal after verify / after uninstall: False / False
  latest-receipt.json survives:              True
  locked config still bridged:               True
  locked config sha unchanged:               True
--- permissions restored ---
  verify    -> ok: true, 4 configs checked
  uninstall -> ok: true
  main / pooled-alpha / pooled-bravo / pooled-charlie: bridged=False mode=0o644   (byte-exact pristine)
  verify    -> InstallError: not installed: no receipt found                       (correct post-uninstall state)
```

**Identical on 3.9.6 and 3.14.6.** No permanent scar: no journal, no lost receipt, no lost baseline, and every
action works normally once the transient condition clears.

**I reproduced the round-4 defect on the baseline to confirm it was real**, same script, `d80d14c517`, Python 3.14.6:

```
  verify    -> ok: true          <-- fail-open
  uninstall -> ok: true          <-- fail-open, deletes latest-receipt.json
  after restoring permissions: verify -> "not installed: no receipt found"
  final: main/alpha/charlie bridged=false 0o644, pooled-bravo bridged=TRUE 0o600
```

The baseline permanently loses the pristine baseline and leaves the handler live — exactly the round-4 report's
claim, and exactly P1-1 resurrected. Round 5 closes it.

### `_path_is_absent()` boundary conditions (`scratchpad/r5/exp_e.py matrix`)

| input | result | correct? |
|---|---|---|
| `ENOENT` (never existed / deleted) | `True` (absent) | yes — the only case that may be "absent" |
| regular file | `False` | yes |
| dangling symlink (raw path) | `False` | yes; in the real flow `resolve()` runs first and the missing target gives `ENOENT` → absent, matching round 4 |
| `ELOOP` symlink cycle | `False` → `validate_owned_file()` then rejects `S_ISLNK` | yes, unchanged from round 4 |
| `EACCES` | `InstallError` | yes — the fix |
| `EIO` (simulated flaky disk) | `InstallError` | yes — the other errno the comment names |
| `ENOTDIR` | `InstallError` | debatable, see R5-P3-C |

Interaction with `resolve_ssd_path()` is safe: `resolve(strict=False)` runs first and normalises `..` and every
readable symlink, so `is_relative_to(root)` still catches escapes; where `resolve()` *cannot* resolve a component
(`EACCES`/`ELOOP`) it leaves it in place and `lstat()`/`validate_owned_file()`'s `O_NOFOLLOW` + `S_ISLNK` checks
catch it. No new escape path.

### No false positives — permanent retirement still works

`scratchpad/r5/exp_e.py enoent`, a real `rm -rf` of an account directory (genuine `ENOENT`, not a rename):

```
  _path_is_absent -> True
  v2 install    -> ok            (row carried forward)
  verify        -> ok: true, retired account reported unreachable: True
  uninstall     -> ok: true, retired account reported unreachable: True
  pending journal: False
  surviving configs pristine: True
  retired dir was NOT resurrected: True
```

R3-P1-A's fix is intact; the new fail-closed behaviour did not turn genuine deletions into refusals.

---

## 5. (b) R4-P2-A — independently verified **FIXED**

My sequence, deliberately not the candidate test's: 4 managed configs; the account parked by **moving its whole
account directory** out of `codex-accounts/` (the test renames `hooks.json` in place); three installs.
`scratchpad/r5/exp_b.py carried`.

```
  v1 install -> ok
  [move codex-accounts/pooled-charlie out of the accounts root]
  v2 install -> ok;  carried-forward row's backup points into the v1 dir: True
  [rm -rf the v1 backup directory]
  v3 install -> ok;  pending journal: False;  parked row still in receipt: True
  verify     -> ok: true
  uninstall  -> ok: true;  parked account reported unreachable: True
  surviving configs pristine: True;  pending journal: False
```

**Baseline `d80d14c517`, same input:**

```
  v3 install -> InstallError: install failed and the durable recovery journal remains pending
  pending journal: True
  verify     -> InstallError: pending install journal must be recovered first
  uninstall  -> InstallError: path unavailable: <pruned v1 backup>
  surviving configs pristine: False
```

Round-4's R4-P2-A reproduced exactly, and round 5 closes it.

---

## 6. (c) Test suite — 70/70 real, both new tests non-vacuous

```
$ cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v
Ran 70 tests in 1.131s
OK
```

31 `install_bridge` methods (up from 29) + 39 `claude_memory_hook` methods = 70. Real, unmocked filesystem work
against a fake SSD; no skips, no expected-failures.

**Non-vacuity audit.** I assembled a package with the **round-4** `install_bridge.py` and the **round-5** test file
and ran the suite. Exactly the two new tests fail, and for exactly the right reasons — and, importantly, they catch
*both* interpreter flavours of R4-P1-A:

| test | 3.9.6 | 3.14.6 | verdict |
|---|---|---|---|
| `test_verify_and_uninstall_fail_closed_on_an_unreadable_managed_config` | ERROR — bare `PermissionError` escapes `verify()` at line 708 | FAIL — `AssertionError: InstallError not raised` at line 707 (`verify()` returned `ok`) | genuinely covers R4-P1-A on both |
| `test_install_survives_a_carried_forward_rows_pruned_backup_directory` | ERROR — `install failed and the durable recovery journal remains pending` at line 760 | same | genuinely covers R4-P2-A |

`Ran 70 tests … FAILED (errors=2)` on 3.9; `FAILED (failures=1, errors=1)` on 3.14. Neither test is a no-op, and
neither passes for an incidental reason.

**Coverage gaps I would still call out**, none of which invalidate the two new tests:

- Neither new test exercises `plan` or `install` while the config is unreadable — which is where R5-P2-A and
  R5-P3-A live. The `0o000` fixture is already in place; two more assertions would have caught both.
- No test covers an unloadable backup on a **non-absent** row — the R5-P1-A hole. The suite has
  `test_uninstall_survives_a_pruned_prior_install_backup_directory` (R3-P2-A, `prev_backup`) and the new
  carried-forward one (R4-P2-A, an `absent` row's `backup`), but nothing for "a row we are actually going to write
  whose `backup` is gone". That is the precondition this round silently dropped.
- No test asserts the negative invariant "a failed `uninstall` leaves no pending journal behind", which would have
  caught R5-P1-A directly.

---

## 7. (d) Regression sweep of rounds 1–4 — clean

Written from scratch against my own fixture, not by re-running the candidate's tests (`scratchpad/r5/exp_d.py`,
`scratchpad/r5/crash.py`). **10/10 pass.**

| # | fix | check | result |
|---|---|---|---|
| 1 | **P1-1** re-install must not poison the uninstall baseline | install → idempotent re-install → upgrade to a new `release_id` → uninstall; every config byte-exact pristine at `0o644` | PASS |
| 2 | **`owned_handler`** exact-token matching | 5 vectors: substring, shell comment, `--other-flag <ID> --bridge-id something-else`, the genuine pair, an unrelated command | PASS |
| 3 | **5+2 guarded `stat`/write sites** | `_mode_bits(missing)`, `atomic_write` into a `0o500` dir, `resolve_ssd_path(missing, must_exist=True)`, `remove_file_durable(missing)` | all `InstallError`, never a bare `OSError` — PASS |
| 4 | **P2-2 / R2-P2-B / schema v2** | 6 malformed receipts (missing `release_id`, missing `backup`, string `before_mode`, `…receipt.v1`, `{}`, truncated) | all `InstallError` — PASS |
| 5 | **R2-P1-B** concurrent-lock directory creation | first `install` on a sandbox with **no** `.shared-runtime`, driven through the real CLI | rc=0, `.shared-runtime` **and** `…/claude-codex-memory-bridge` both `0o700` — PASS |
| 6 | **P2-3** concurrency | hold the `flock`, run a second CLI `uninstall` | rc=1, `"another install_bridge.py invocation is already running"` — PASS |
| 7 | **R2-P1-A** `prev_*`/`before_*` separation | v1 install → upgrade whose **second** config write fails | rolled back to **v1 (prev)**, not pristine; no journal left; `verify`/`uninstall` still work afterwards — PASS |
| 8 | **P1-2** uninstall transaction journal | real `SIGKILL` (signal 9) after the first config revert, then a fresh-process `recover` | `recover` → `{"state": "uninstalled"}`, all configs byte-exact pristine, `latest-receipt.json` cleared — PASS |
| 9 | **R3-P1-A** permanently retired account | `rm -rf` an account dir, re-install, verify, uninstall | tolerated as `absent`/`unreachable`, no resurrection, survivors pristine — PASS (§4) |
| 10 | **R3-P2-A** pruned prior backup directory | install → upgrade → `rm -rf` the **prior** transaction's backup dir → uninstall | ok, configs pristine, no journal — PASS |

No regressions. The four P2 bare-exception protections (`atomic_write`, `_mode_bits`, `resolve_ssd_path`,
`remove_file_durable`) are all still in place and still guarded.

---

## 8. (e) What else I examined and cleared

- **Every consumer of the now-lazy `backup`.** `_receipt_rows()` has exactly two callers (`:714`
  `recover_pending_install()`, `:1099` `uninstall()`), and `backup_obj` is consumed at exactly two places
  (`:782`, `:1125`) — both now load lazily. No caller needed the *bytes* eagerly. The dependency that broke was
  the **ordering** one, not a data one: `uninstall()` relied on `_receipt_rows()` validating backups before
  `atomic_write(PENDING_PATH, …)` at `:1116`. That is R5-P1-A.
- **`install()` is not affected by the laziness.** It reads `previous_row["backup"]`/`["after_backup"]` directly
  from the receipt dict with `must_exist=True` (`:898`, `:906`) for **discovered** paths only, so discovered rows
  are still eagerly validated and carried-forward rows are still exempt — exactly the R4-P2-A intent.
- **`recover_pending_install()`'s install branch** uses `prev_backup`, lazy since round 3 — unchanged, and it
  cannot create a wedge because the journal already exists when it runs.
- **`_path_is_absent()` vs `resolve_ssd_path()`** — full interaction matrix in §4; no new escape path, no new
  symlink hole.
- **The other unguarded `exists()`/`is_symlink()` sites** (`:196`, `:444`, `:699`, `:718`, `:814`, `:1014`,
  `:1017`, `:1161`) all live in the runtime directory rather than in managed account homes. I traced the dangerous
  ones: with `RUNTIME_BASE` unreadable, `install`/`uninstall`/`recover` are pre-empted by
  `_acquire_exclusive_lock()`'s `ensure_private_dir()` chain, which fails closed on the `mkdir`/`open`; `verify`
  degrades to the misleading-but-still-`ok:false` `"not installed: no receipt found"`; `plan` reports
  `pending_transaction: false` fail-open. All identical to round 4 — this is R3-P3-B, unchanged and still
  unreachable on the target machine.
- **Digest/mode invariants.** `_load_backup()` still digest-checks against `before_sha256`, and every write is
  still followed by a read-back comparison of both bytes and mode (`:1127–1130`, `:784–787`). The lazy change did
  not weaken either.
- **Receipt schema.** `RECEIPT_SCHEMA` correctly stays at `v2` — the receipt's shape did not change this round.
- The working tree matches `9e2205dc43` for the reviewed directory (`git diff 9e2205dc43 -- claude-codex-memory-bridge/`
  is empty), so what I reviewed is what would be shipped.

---

## 9. Recommendation

**NO-GO** for `9e2205dc43`.

Fix **R5-P1-A** (validated six-line patch in §1; apply the same shape to `recover_pending_install()`'s uninstall
branch) and **R5-P2-A** in the same batch, and add the two regression tests the suite is missing:

1. an `uninstall` whose backup is unloadable on a **non-absent** row must leave **no** pending journal and must not
   revert anything — assert `PENDING_PATH` does not exist after the refusal;
2. `plan` and `install` under the round-5 test's existing `0o000` fixture must return clean JSON, not a bare
   traceback (3.9) and not a wedged journal (3.14).

Then re-review the new candidate. R4-P1-A and R4-P2-A are genuinely closed and should not need re-litigating; the
rounds 1–4 sweep is clean and can be spot-checked rather than repeated in full.

One process observation, offered as a reviewer and not as a gate: five consecutive rounds have each fixed the prior
finding correctly and introduced a new one, and four of the five new defects — including this one — were
*lockouts created by the recovery machinery itself*. The recurring shape is that a check gets moved, relaxed, or
made lazy without anyone asking which caller was implicitly depending on it running when it did. Before the next
candidate, it may be worth writing down `uninstall()`'s and `install()`'s preconditions explicitly ("nothing becomes
durable until every byte this transaction will write has been read and digest-checked") and asserting them in tests,
rather than continuing to rediscover them one round at a time.

---

### Reproduction artifacts

All under
`/Volumes/Extreme SSD/Orca/tmp/claude-code-runtime/claude-501/-Volumes-Extreme-SSD-Orca-workspaces-orca---orca/0ac70dac-7417-43c6-ad4e-ef4854135cb2/scratchpad/r5/`:

| file | purpose |
|---|---|
| `harness.py` | isolated fake-SSD sandbox (4 configs, my own account names/layout) |
| `cli.py` / `crash.py` | drive the real `main()` CLI; SIGKILL mid-uninstall |
| `exists_probe.py` | interpreter matrix for `exists`/`is_symlink`/`is_file`/`lstat` under `EACCES` |
| `exp_a.py readonly \| plan_install` | R4-P1-A verification; R5-P2-A / R5-P3-A |
| `exp_b.py carried \| returns` | R4-P2-A verification; R5-P1-A's partial-revert flavour |
| `exp_c.py rm \| chmod` | R5-P1-A minimal trigger, both recoverability flavours |
| `exp_cli.py` | R5-P1-A through the real CLI, round 5 vs round 4 |
| `exp_d.py` | rounds 1–4 regression sweep (10 checks) |
| `exp_e.py matrix \| enoent \| device` | `_path_is_absent()` boundaries; no-false-positive; R5-P3-B |
| `enotdir.py` | R5-P3-C |
| `vacuity/` | round-4 code + round-5 tests (non-vacuity audit) |
| `fixcheck/` | round-5 code + the illustrative R5-P1-A fix (**scratchpad copy only**) |
