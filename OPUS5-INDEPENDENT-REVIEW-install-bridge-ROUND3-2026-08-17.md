# Independent read-only review — `install_bridge.py` round 3

- **Reviewer**: Claude opus5 / max effort, independent worker (`task_516b2a97faa8`), blind to the parallel round-3 reviewer
- **Candidate**: `870810d468` — `claude-codex-memory-bridge/install_bridge.py` + `tests/test_install_bridge.py`
- **Round-2 baseline for the delta**: `09459cf6a0`
- **Date**: 2026-08-17
- **Mode**: read-only. Nothing was modified, committed, merged, installed or activated. Every reproduction ran in throwaway sandboxes under the session scratchpad with a **fake** SSD root (`<scratch>/r3/work/*/fake-ssd`) and monkeypatched module constants, always in separate child processes. **No `install`/`uninstall`/`recover` was ever run against the real `/Volumes/Extreme SSD` Codex hook configs.** Real-filesystem access was limited to `ls`/`stat`/`readlink`-class reads of `local-homes`, `codex-accounts`, `.shared-runtime` and `~/.codex`.
- **Harness**: written from scratch for this round (`<scratch>/r3/harness.py`, `exp_a_*`, `exp_a2_*`, `exp_b_*`, `exp_cd_*`, `exp_fg_*`, `exp_extra.py`). Deliberately unlike the candidate suite's fixture: **4** hook configs instead of 2, account names `pooled-alpha` / `pooled-beta` / `pooled-gamma` (never `acct-one`/`acct-two`), two pre-existing unrelated handlers plus a second event key per config, and interruption points selected **semantically** (by *which file is about to be written*) rather than by the candidate's magic `atomic_write` call number.

---

## Verdict: **NO-GO**

All three round-2 P1s and both round-2 P2s are **genuinely fixed**. I reproduced every one of them on the round-2 baseline `09459cf6a0` with my own fixtures — including two I hit incidentally before I even tried — and confirmed each is closed on `870810d468`. The `prev_*` / `install_state` design is sound, and it introduces **no** false positive on the first-install path: all five first-install SIGKILL points still behave exactly as round 1's verified rollback-to-pristine.

But **this round introduces a new P1 of its own**, and it is a functional regression against `09459cf6a0`:

| ID | Introduced by | Effect | Repro needs |
|----|---------------|--------|-------------|
| **R3-P1-A** | fix ② (Codex P1-R2-1 fail-closed check) | Permanently deleting or renaming a pooled Codex account after install locks `install`, `uninstall` **and** `verify` out forever. The bridge stays active in every remaining `hooks.json`; `plan` still reports `ok: true`; the error text's own instruction ("run uninstall first") provably cannot be followed. Round 2 recovered from the identical scenario cleanly. | deleting one `codex-accounts/<uuid>/` directory. No crash, race or privilege. |

This is the **fourth consecutive round** in which the round's own fix introduces a new defect of the same family — an all-actions-refuse lockout where only `plan` reports success — and it is exactly the signature both prior rounds classified as P1.

Two further P2/P3 items and one factual correction to the commit message's own claims are below.

---

## 1. Round-2 findings — independently confirmed fixed

### (a) R2-P1-A — interrupted **upgrade** install — **FIXED**

`exp_a_upgrade_sigkill.py`. Fixture: 4 configs, v1 installed normally, source script then upgraded (new `release_id` ⇒ new command), then the upgrade install **SIGKILLed for real** (`os.kill(os.getpid(), SIGKILL)`, child `returncode == -9` asserted every time) at four semantic points I chose myself. Recovery always driven from a **fresh process** via the real `main("recover")`.

| Kill point | on-disk after kill | `recover` | resulting state | `verify` | `plan` | `install` | `uninstall` |
|---|---|---|---|---|---|---|---|
| before config #1 is rewritten | 0/4 at v2 | `rolled_back` | **== v1 exactly** | 0 | ok | 0 | 0 |
| **mid-loop** (1/4 at v2, 3/4 still v1) | genuine mixed state | `rolled_back` | **== v1 exactly** | 0 | ok | 0 | 0 |
| after all 4 written, before `latest-receipt.json` | 4/4 at v2 | `rolled_back` | **== v1 exactly** | 0 | ok | 0 | 0 |
| after `latest-receipt.json`, before journal removal | 4/4 at v2 | `committed` | v2 | 0 | ok | 0 | 0 |

In every rolled-back case the harness asserts both `recover_equals_v1 == True` **and** `recover_equals_pristine == False` — i.e. it lands on the previously-working install, not on pristine and not half-upgraded — and the final `uninstall` still returns to byte-exact pristine.

**Non-vacuity, established independently.** Same script, same fixture, same kill points, against `09459cf6a0` (with `.shared-runtime` pre-created at `0o700` so the baseline can get past R2-P1-B and actually reach this code path):

| Kill point | round-2 `recover` | round-2 `verify` / `install` / `uninstall` / `plan.ok` |
|---|---|---|
| before config #1 | `{"ok": false, "error": "pending install cannot roll back because a config drifted"}` | 1 / 1 / 1 / **false** |
| mid-loop | same permanent refusal | 1 / 1 / 1 / **false** |
| before `latest-receipt.json` | `rolled_back` — but **all the way to pristine** (`recover_equals_pristine: True`), silently uninstalling the working v1 | 1 / 1 / 0 / true |
| after `latest-receipt.json` | `committed` | 0 / 0 / 0 / true |

Both halves of R2-P1-A reproduce exactly as reported: the permanent drift lockout, and the over-rollback that silently removes a working install. Note the round-2 lockout fires even when **nothing at all was touched** (kill before config #1) — the disk is a perfectly consistent v1 install and the tool still refuses forever.

**Recovery is also idempotent** (`exp_fg_regressions.py recover_sigkill`): interrupting an upgrade, then SIGKILLing the *rollback itself* halfway through, then running `recover` again → `rolled_back`, state `== v1 installed`, `verify` 0, `uninstall` 0, final pristine.

**No false positive on first install** (`exp_a2_first_install_sigkill.py`, 5 points × real SIGKILL, no prior receipt):

| Kill point | `recover` | state after recover | subsequent `install`→`uninstall` |
|---|---|---|---|
| during the backup writes (before the journal exists) | `none` | pristine | 0 / 0, final pristine |
| before config #1 | `rolled_back` | **pristine** | 0 / 0, final pristine |
| mid-loop (1/4 written) | `rolled_back` | **pristine** | 0 / 0, final pristine |
| before `latest-receipt.json` (4/4 written) | `rolled_back` | **pristine** | 0 / 0, final pristine |
| after `latest-receipt.json` | `committed` | installed | 0 / 0, final pristine |

`prev == before` on a first install, so the new `install_state` classification is behaviourally identical to round 1's SIGKILL-verified rollback. Confirmed, not assumed.

### (b) Codex P1-R2-1 — a managed config goes undiscovered — **FIXED**

`exp_b_discovery.py`, my own account (`pooled-beta`) and my own rename targets (never the candidate's `hooks.json.missing`):

| scenario | `install` while missing | after the path returns |
|---|---|---|
| `vanish_return` — `hooks.json` renamed to `hooks.json.maintenance-swap` and back | `{"ok": false, "error": "the following previously-managed hook configs are no longer discoverable …"}`, rc 1, config untouched | `install` 0 → `uninstall` 0 → **all 4 configs byte-exact pristine**, `pooled-beta` back to its 2 original handlers |
| `vanish_dir` — the whole `codex-accounts/pooled-beta/` directory renamed away and back (my own variant, not in the suite) | same clean refusal | same clean full restore |
| `new_account` — a brand-new `pooled-delta` appears after install (additions only) | n/a | `install` 0 picks it up (5/5 configs bridged), `verify` 0, `uninstall` 0, **all 5 byte-exact pristine** |

The "only additions" case is genuinely unaffected — no false positive.

**Non-vacuity**: same `vanish_return` script against `09459cf6a0` → `install_while_missing` **rc 0** (silently dropped the account), then after the file returned `install` rc 0 and `uninstall` rc 0 both reporting `"ok": true`, while `pooled-beta` was left with **3 handlers — the bridge handler still installed** (`final_equals_pristine: false`). Exactly the reported bug.

### (c) R2-P1-B — fresh machine, no `.shared-runtime` — **FIXED**

Driven through the real `main()` with a real `argv`, in a child process, on a fixture where `.shared-runtime` genuinely does not exist (`exp_cd_lock_and_p2.py fresh_install`):

| | `870810d468` | `09459cf6a0` |
|---|---|---|
| `main("install")` | **rc 0** | rc 1 `{"ok": false, "error": "unsafe runtime directory: …/.shared-runtime"}` |
| `.shared-runtime` mode | **`0o700`** | **`0o755`** ← the bug, verbatim |
| `claude-codex-memory-bridge` / `releases` / `backups` | `0o700` / `0o700` / `0o700` | `0o700` / absent / absent |
| `installer.lock` | `0o600` | `0o600` |
| configs bridged | 4/4 | 0/4 |
| follow-up `verify` / `uninstall` | 0 / 0 | 1 / 1 |

I hit R2-P1-B *before I went looking for it* — my very first upgrade experiment on the baseline failed at step 1 with this exact error, which is an unprompted independent reproduction. Confirmed on the real machine (read-only): `/Volumes/Extreme SSD/Orca/local-homes/.shared-runtime` **does not exist**, so the first real `install` would have hit it.

### (d) The two P2s — **FIXED**

Everything driven through the real `main()`; `crashed_with_traceback` measured from the child's stderr.

| probe | `870810d468` | `09459cf6a0` |
|---|---|---|
| `RUNTIME_BASE` at `0o500` → lock `os.open(O_CREAT)` fails | rc 1, clean `{"ok": false, "error": "cannot open lock file: …"}` | **bare `PermissionError` traceback, no JSON at all** |
| `.shared-runtime` is a regular file → the mkdir path | rc 1, clean `{"ok": false, "error": "unsafe runtime directory: …"}` | **bare `NotADirectoryError` traceback, no JSON at all** |
| `installer.lock` replaced by a symlink | rc 1, clean refusal (`O_NOFOLLOW`) | **rc 0 — follows the symlink and installs anyway** |
| receipt with only `release_id` deleted → `verify` | rc 1, clean `{"ok": false, "error": "invalid receipt"}` (and `uninstall`/`install` likewise) | **bare `KeyError: 'release_id'` traceback out of `verify()`** |

Both P2s are closed, and the `O_NOFOLLOW` addition is real hardening beyond what was asked for.

---

## 2. Round-1 / round-2 items re-checked for regression — **no regressions**

| item | check | result |
|---|---|---|
| **P1-2** uninstall transaction journal | real SIGKILL mid-uninstall (mixed state: 1 config reverted, 3 still installed) and just before `latest-receipt.json` removal, recovery from a fresh process | `recover` → `uninstalled` both times; all 4 configs byte-exact pristine; `verify` → `not installed`; subsequent `install`→`uninstall` still exact |
| **`owned_handler` exact matching** | 12 reviewer-authored vectors: exact generated form, superstring value, substring value, `--my-bridge-id`, bare id, post-`#` comment, single-quoted whole arg, `--bridge-id=` equals form, match in a later hook, unbalanced quote, non-dict handler, non-list `hooks` | 12/12 correct, identical to round 2. `update_hook_config(remove=True)` deletes only the genuine handler and leaves both decoys byte-intact |
| **the guarded `stat` sites** | code audit of all 20 `stat`/`lstat`/`exists`/`iterdir` call sites + runtime probes | `resolve_ssd_path`, `_mode_bits`, `validate_owned_file`, `ensure_private_dir`'s `lstat`, `discover_hook_configs`'s `iterdir`/`lstat` all still guarded |
| **P2-3 concurrency lock** | two **real** concurrent `install` processes (writer slowed by 0.6 s per config) | winner rc 0 and fully installed; loser rc 1 `{"ok": false, "error": "another install_bridge.py invocation is already running"}`, no traceback, no interleaving; `verify` 0 and `uninstall` 0 afterwards |
| **P1-1 baseline inheritance** | idempotent re-install, upgrade re-install, new-account-mid-life, all followed by `uninstall` | byte-exact pristine restore in every case |

---

## 3. New findings

### R3-P1-A — deleting a pooled Codex account permanently locks out `uninstall` and `verify` — **P1, new this round, regression vs `09459cf6a0`**

**What happens.** `install()`'s new fail-closed check refuses when any path in `latest-receipt.json` is not rediscovered. That is the right call for the *temporary* disappearance Codex P1-R2-1 described. But nothing handles the *permanent* case, and the two other actions that could clear the receipt were already unable to run with a missing path:

- `uninstall()` → `_receipt_rows()` → `resolve_ssd_path(row["path"], must_exist=True)` → `InstallError: path unavailable: …`
- `verify()` → the same resolve → `InstallError: path unavailable: …`

So every state-changing action refuses, permanently:

```
install    → rc 1  {"ok": false, "error": "the following previously-managed hook configs are no longer
                    discoverable and cannot be safely carried forward by install (run uninstall first): …"}
uninstall  → rc 1  {"ok": false, "error": "path unavailable: …/pooled-beta/home/hooks.json"}
verify     → rc 1  {"ok": false, "error": "path unavailable: …/pooled-beta/home/hooks.json"}
recover    → rc 0  {"ok": true, "state": "none"}
plan       → rc 0  ok: true
```

and the three surviving configs are left with the bridge handler installed (3 handlers each), executing on every Codex prompt, with no supported way to remove it. `install`'s own instruction — *"run uninstall first"* — is the one action that provably cannot run. Recreating the account directory does not help (`uninstall_after_empty_dir_restored` → same `path unavailable`); only hand-editing the transaction store would, and deleting `latest-receipt.json` by hand reintroduces round 1's P1-1 baseline poisoning.

**Why it is a regression, not merely an unhandled edge.** Identical scenario, `09459cf6a0`:

```
install    → rc 0     (silently drops the deleted account from the new receipt)
verify     → rc 0
uninstall  → rc 0     → all 3 surviving configs restored to byte-exact pristine (2 handlers each)
```

Round 2 recovered cleanly. Round 3 cannot. The round-2 behaviour was unsafe *for the temporary case* — that is Codex P1-R2-1 and it had to be fixed — but the fix removed the only working escape from the permanent case without adding one.

**Reachability on the real target machine.** `/Volumes/Extreme SSD/Orca/local-homes/codex-accounts/` currently holds two **UUID-named** account homes (`0b4cd443-…`, `b9f32a51-…`), each with a real `home/hooks.json`. Those directories are provisioned and reclaimed by the pooled-account tooling; a re-provisioned account arrives under a *new* UUID, which is indistinguishable from deletion + addition as far as `discover_hook_configs()` is concerned. Reproducing needs no crash, no race, no privilege — just `rm -rf` (or a rename) of one account directory between two installs.

**Exact repro** (`<scratch>/r3/exp_extra.py`, case `deleted_account_uninstall_first`):

1. fixture: `local-homes/.codex/hooks.json` + `codex-accounts/{pooled-alpha,pooled-beta,pooled-gamma}/home/hooks.json`
2. `main("install")` → rc 0, 4/4 bridged
3. `shutil.rmtree(codex-accounts/pooled-beta)`
4. `main("uninstall")` → rc 1 `path unavailable`; `main("verify")` → rc 1; `main("install")` → rc 1 `run uninstall first`; `main("plan")` → rc 0 `ok:true`
5. surviving configs: `{"main": 3, "pooled-alpha": 3, "pooled-gamma": 3}` — still bridged, forever

Ordering is not a factor: the same result holds whether `uninstall` is tried first or after a refused `install`.

**Not fixing it here** (read-only review), but the shape of a fix: `uninstall()` should be able to complete over the paths it *can* still reach and report the unreachable ones explicitly rather than aborting the whole transaction, and/or `install()` should carry a missing path's previous receipt row forward untouched (preserving the real `before_*` baseline, which is exactly what Codex P1-R2-1 was about protecting) instead of refusing. Either restores a supported exit; both together would be better.

### R3-P2-A — `uninstall` now depends on files it never uses, in a *previous* transaction's backup directory — **P2, new this round**

`_receipt_rows()` is shared by `uninstall()` and by `recover_pending_install()`'s install branch, and it now unconditionally resolves, reads and digest-checks **three** files per row: `backup`, `prev_backup` and `after_backup`. `uninstall()` only ever uses `backup_raw`. And unlike `backup`, which `install()` always writes fresh into the *current* transaction's directory, `prev_backup` is a **pointer into the previous install's backup directory** (`prev_backups[path] = prior_after_backup`).

Consequence (`exp_fg_regressions.py stale_backup_dir_gone`): install v1, upgrade to v2, then delete v1's backup directory —

```
verify    → rc 0   (ok)
uninstall → rc 1   {"ok": false, "error": "path unavailable: …/backups/<install-1>/<digest>-after.json"}
install   → rc 0   (succeeds; the new receipt re-points prev_backup at v2's still-present directory)
```

`uninstall` is blocked by the absence of a file it does not read, from a transaction that already completed. It is escapable — running `install` again re-points the pointer — but that escape is undocumented and counter-intuitive, and `backups/` is a directory the code itself notes is *never pruned*, so it is exactly the kind of thing an operator eventually cleans up.

Two related dead-code observations found while tracing this: `_receipt_rows()` stores `after_backup_raw`, `after_backup_obj` and `prev_backup_obj` in every row and **nothing anywhere reads them**; and `install()`'s `prev_raw` dict is populated on both branches and never read. So the `after_backup` read + digest check inside `_receipt_rows()` (lines 549–552) buys nothing that `install()` does not already re-verify for itself at lines 798–800 — it only adds failure surface to `uninstall`/`recover`. The commit's stated rationale for `after_backup` ("so a fresh recovery process can finish … an interrupted install without having to re-derive them") is not actually realised: recovery never rolls *forward* to `after`, only back to `prev` or straight to `committed`. The `after_backup` **file** does earn its place — as the thing the *next* receipt's `prev_backup` points at — but reading it in `_receipt_rows()` does not.

### R3-P3-A — the `.shared-runtime` fix prevents the bad mode but never heals one — **P3, not currently reachable**

`_acquire_exclusive_lock()` now builds the chain through `ensure_private_dir()`, which is correct and verified above. But if `.shared-runtime` **already** exists at `0o755` — precisely the state the round-2 build leaves behind after its own failed first `install` — the round-3 code still refuses permanently and never chmods it back:

```
install       → rc 1 {"ok": false, "error": "unsafe runtime directory: …/.shared-runtime"}
install again → rc 1  (same)
mode before / after: 0o755 / 0o755
```

The commit comment names "nothing ever chmods it back" as the failure mode and then only fixes the creation path. **Not reachable today**: `.shared-runtime` does not exist on the target machine and the round-2 build was never run there, so this is a latent gap, not a blocker. It becomes a blocker the moment anyone runs an older build once.

### R3-P3-B — bare `OSError` can still escape `main()` — **P3, pre-existing, unchanged this round**

The P2-1 / R2-P2-A line of fixes aims at "no failure mode leaves `main()` as a raw traceback". It is not complete. `ensure_private_dir()`'s `path.exists()`, `write_runtime()`'s `path.exists()`, `read_receipt()`/`plan()`/`recover_pending_install()`'s `PENDING_PATH.exists()` and `discover_hook_configs()`'s `candidate.is_file()` are all unguarded, and `pathlib` only swallows `ENOENT`/`ENOTDIR`/`ELOOP`-class errors — `EACCES` propagates.

Concrete probe (`exp_extra.py runtime_mode_000`): a `.shared-runtime` directory at mode `0o000` passes `ensure_private_dir`'s own safety test (`st_mode & 0o077 == 0`), and then **all five actions** — `install`, `uninstall`, `recover`, `verify`, `plan` — die with a bare `PermissionError` traceback and empty stdout. Identical on `09459cf6a0`, so this round neither introduced nor worsened it; routing `_acquire_exclusive_lock()` through `ensure_private_dir()` just moves the crash a few lines earlier. Recording it so the "clean JSON on every failure" claim is not over-trusted.

---

## 4. Test suite (task item **e**)

**66/66 pass**, verified: `cd claude-codex-memory-bridge && /usr/bin/python3 -m unittest discover -s tests -v` → `Ran 66 tests … OK` (0.62 s, Python 3.9.6).

**Non-vacuity, checked directly.** I assembled a package with the **round-2** `install_bridge.py` and the **round-3** test file and ran the suite against it. Exactly four tests fail, and they are the four that matter:

| test | vs `09459cf6a0` | covers |
|---|---|---|
| `test_interrupted_upgrade_install_recovers_to_the_previous_working_state` | **ERROR** | R2-P1-A |
| `test_install_refuses_when_a_previously_managed_config_goes_undiscovered` | **FAIL** | Codex P1-R2-1 |
| `test_main_install_succeeds_end_to_end_when_shared_runtime_does_not_yet_exist` | **FAIL** | R2-P1-B |
| `test_corrupted_receipt_row_raises_install_error_not_a_bare_exception` | **ERROR** | R2-P2-B — the previously-vacuous test, now genuinely non-vacuous |

No no-op tests among the changes. The one test the previous round flagged as vacuous really was rewritten into a real one.

Coverage gaps worth naming, none blocking on their own:

- **R2-P2-A has no test.** The `mkdir`/`os.open` guarding in `_acquire_exclusive_lock()` is exercised by nothing in the suite; `test_concurrent_installer_invocations_fail_the_lock_instead_of_interleaving` passes unchanged against the round-2 baseline. I verified the guard myself (§1(d)); the suite would not notice if it regressed.
- **The upgrade-interruption test uses the weakest of the interruption points.** `calls["n"] == 7` is, for the 2-config fixture, the first *live config* write — so the test kills before any config is rewritten and never produces a genuinely mixed "one config at v2, one still at v1" state. Its own comment is honest about this. The mixed state is the harder case; I covered it separately (§1(a) mid-loop) and it passes. The magic constant is also brittle: adding or removing any `atomic_write` call before the config loop silently retargets the test.
- **No test covers R3-P1-A, R3-P2-A or R3-P3-A.**

**Factual correction to the commit message.** It states *"New regression tests (5) … 66/66 tests pass (61 prior + 5 new)"*. Verified counts: the `09459cf6a0` suite is **63** tests (all passing), the `870810d468` suite is **66**, and `tests/test_install_bridge.py` goes from **24** to **27** test methods (`tests/test_claude_memory_hook.py` unchanged at 39). So it is **+3 new tests plus 1 rewritten**, from a 63-test baseline — not "61 prior + 5 new". A counting error rather than a substantive one, but the stated coverage should not be relied on as written.

---

## 5. What I checked and found clean

Beyond the items above: idempotent re-install (no handler duplication, `install_state == "both"`, commit path clean); upgrade + new-account-in-the-same-transaction (the new account correctly rolls back to *its* pristine state while existing ones roll back to `prev`); rollback ordering (`reversed(rows)`); receipt canonicalisation and the `latest_matches_this_receipt` byte comparison; `after_mode` pinned to `0o600` in `_validate_receipt_shape`; mode restoration through the whole cycle (`0o644` → `0o600` installed → `0o644` after uninstall); the `chmod`-only drift case (all three actions refuse, but honestly and with an obvious manual fix — `verify` correctly reports `file is not private` rather than a false `ok`, so this is not a repeat of the R2-P1-A pattern; pre-existing, informational only).

---

## 6. Bottom line

The three fixes this round are correct, well-targeted and independently verified — including under harder conditions than the candidate's own tests use, and including a demonstration that the round-2 defects were real. If R3-P1-A did not exist I would sign this off.

R3-P1-A does exist, it is reachable by an ordinary pooled-account operation on the very machine this is destined for, it leaves the bridge running with no supported way to remove it, and it is a regression against the code it replaces. Per the standing rule that no reproducible P0/P1 may be completed, merged, installed or deployed: **NO-GO**, re-review required for the new candidate.
