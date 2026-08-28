# Independent read-only review — `install_bridge.py` round 6

**Reviewer:** Claude `opus` / effort `max`, independent read-only pass
**Candidate:** `f8abefc9f0` (`fix(install_bridge): load every backup a transaction will write from before writing its journal`)
**Baseline:** `9e2205dc43` (round-5 candidate)
**Scope:** `claude-codex-memory-bridge/install_bridge.py`, `claude-codex-memory-bridge/tests/test_install_bridge.py`
**Date:** 2026-08-17

**Reviewed bytes.** `install_bridge.py` → `c98b9ff5427ff3e127b0434825530ee2cfbf974fa1382b30f43b316ec3e4b357`,
`tests/test_install_bridge.py` → `cfba4d3e394b1c2158f5c8657c978b21950bc9fe2c68c40e852f69dcce2fdc6b`.
`HEAD` is `d7c73e8b71` (a docs-only commit on top of the candidate); `git diff f8abefc9f0 -- claude-codex-memory-bridge/`
is empty, so the working tree I exercised *is* the candidate.

**Nothing was modified, committed, merged, installed, or activated.** Every reproduction ran inside a throwaway
`tempfile.TemporaryDirectory()` sandbox with `SSD_ROOT` / `LOCAL_HOMES_ROOT` / `RUNTIME_BASE` / `PENDING_PATH` /
`SOURCE_SCRIPT` / `Path.home` / `volume_uuid` rebound to that sandbox. The real
`/Volumes/Extreme SSD/Orca/local-homes` was read for **layout only** (`ls`); it still has **no `.shared-runtime`**,
i.e. this bridge has never been installed on the target machine, and no `install` was ever run against it.
I did not read the parallel round-6 Codex review file, which exists in the repo root, at any point.

---

## Verdict: **NO-GO**

**Both round-5 findings are genuinely and completely fixed**, and I verified each independently with my own
scenarios (4 managed configs instead of 3, an *upgrade* install instead of a first install, my own account names,
three additional failure variants the candidate suite does not have, and a strengthened version of the candidate's
own second test that can actually discriminate what it claims to). The rounds 1–5 regression sweep is clean.
72/72 on both interpreters, both new tests are non-vacuous, and — for the first time in this series — **I found no
defect introduced by the round-6 diff itself**. The two eager-load sites cover exactly the rows their write loops
consume, and the new carried-forward pre-check is placed correctly (before every durable write).

The NO-GO is for something else: while probing the carried-forward machinery from angles the previous five rounds
did not use, I hit a **pre-existing P1 that no round has reported** and that reproduces with a single `mv` — no
crash, no race, no privilege, no permission games, no mock, on both interpreters, on the candidate *and* on the
round-5 baseline. It is the round-1 **P1-1** failure verbatim — `uninstall` reports `ok: true`, lists the
account in `restored`, and leaves the bridge handler executing — reached through a door round 1's fix does not
cover: a managed config that shows up under a **new path** while still holding our handler.

Because the candidate's purpose is to be installed on the real machine, and this file's single most important
invariant ("a reported-successful uninstall really removed the bridge") is reproducibly violated, it cannot ship as
is. This is a *pre-existing* defect, not a round-6 regression — the round-6 diff is, as far as I can determine,
correct and net-positive.

| id | severity | status |
|---|---|---|
| **R6-P1-A** — a renamed/moved account path makes `install()` adopt already-bridged content as the "pristine" baseline; `uninstall()` then reports `ok:true` and leaves the bridge live and undetectable forever | **P1, blocking** | **pre-existing, never reported (rounds 1–5)** |
| R6-P2-A — R5-P2-A's fix covers only ENOENT-vs-indeterminate; a carried-forward path that *exists but is unusable* still produces the identical stuck-journal wedge | P2 | residual of R5-P2-A |
| R6-P2-B — on the documented `/usr/bin/python3` 3.9, R5-P2-A's own reported trigger never reaches the new pre-check: `main("plan")` / `main("install")` die with an **uncaught `PermissionError` traceback** | P2 | pre-existing (= R5-P3-A), now interpreter-divergent |
| R6-P2-C — `recover_pending_install()`'s install-direction rollback also reverts **carried-forward rows the interrupted install never touched** | P2 | pre-existing (round 3+), never reported |
| R6-P3-A — fix ① applied to `recover`'s uninstall branch but not its install branch (`prev_backup` still lazy) | P3 | asymmetry introduced this round |
| R6-P3-B — the second new test cannot discriminate its own headline claim ("neither discovered config was rewritten") | P3 | test quality, this round |
| R6-P3-C — `verify()` reports duplicated paths and `ok:true` when several receipt rows resolve to one file | P3 | pre-existing |
| **R5-P1-A** | — | **FIXED** (verified independently, both interpreters, 3 extra variants) |
| **R5-P2-A** | — | **FIXED** for the indeterminate axis (verified with a strengthened, discriminating probe) |
| rounds 1–5 confirmed fixes | — | **no regressions** (9/9 independent checks) |

---

## 0. Environment, interpreter matrix, harness

| | |
|---|---|
| documented interpreter | `/usr/bin/python3` → **3.9.6** (this is what `make_release()` bakes into the hook command line and what the suite is run with) |
| PATH interpreter | `/opt/homebrew/bin/python3` → **3.14.6** |
| candidate suite | **72/72 OK on 3.9.6**, **72/72 OK on 3.14.6** (33 `install_bridge` methods + 39 `claude_memory_hook` methods) |
| test isolation | 33/33 also pass with the class's methods run in **reverse alphabetical order**, and both new tests pass **standalone** — no ordering or shared-state dependency |
| real target machine | `/Volumes/Extreme SSD/Orca/local-homes/.shared-runtime` **does not exist**; `codex-accounts/` holds two UUID-named homes (`0b4cd443-…`, `b9f32a51-…`), each with a real `home/hooks.json` |

**Harness** (`<scratch>/harness.py`, `scenario_a.py`, `scenario_b.py`, `scenario_c.py`, `scenario_d.py`,
`scenario_e.py`, `probe_b5.py`, `probe_cli.py`, `probe_precheck.py`) was written from scratch for this round and
deliberately does not reuse the candidate suite's fixture: **4** managed configs (`.codex` + `pool-charlie`,
`pool-alpha`, `pool-bravo`) instead of 3, a distinct non-trivial pristine hook (`/bin/echo pre-existing-site-hook`)
so "an unrelated site hook survived" is checked separately from "our handler is gone", `bump_release()` so most
scenarios run as **upgrades** rather than first installs, and real `SIGKILL`ed child processes for the crash paths.
A byte-identical copy of the round-5 baseline (`git show 9e2205dc43:…`) was driven through **the same scripts** so
every "fixed" claim below is a candidate-vs-baseline delta, not an assertion.

---

## 1. (a) R5-P1-A — **FIXED**, verified independently

### 1.1 My scenario (deliberately unlike the candidate test)

`scenario_a.py`: four managed configs; `install` → `bump_release("v2")` → `install` (so every row carries a
non-trivial `prev_backup` pointing at the *first* transaction's after-backup, which the candidate test never
exercises) → `shutil.rmtree()` of **this second transaction's own backup directory** (permanent loss, not a
permission trick) → `uninstall`.

| | candidate `f8abefc9f0` | baseline `9e2205dc43` |
|---|---|---|
| `uninstall` result | `InstallError(cannot read …/backups/<id>/<digest>.json)` | `InstallError(uninstall failed and the durable recovery journal remains pending)` |
| pending journal on disk | **False** | **True** |
| `latest-receipt.json` | intact | intact |
| every config byte-identical to pre-uninstall | **True** | True (this variant), **False** below |
| `plan` | `ok:true` | `ok:false` |
| `verify` | `ok:true` | `InstallError(pending install journal must be recovered first)` |
| `recover` | `{"ok":true,"state":"none"}` | `InstallError(cannot read …)` — **the wedge** |

Two extra variants the candidate suite does not have, both on the candidate:

* **one row's backup file deleted** (not the whole directory) → clean refusal, no journal, **all four configs
  byte-identical**. On the baseline: journal stuck **and** `every config byte-identical: False` — i.e. the
  half-uninstalled state R5-P1-A describes, with earlier rows already reverted.
* **one row's backup present but corrupted** (digest mismatch) → candidate: `transaction backup digest mismatch`,
  nothing written. Baseline: journal stuck, partial revert.

### 1.2 The "permanently lost backup" worst case is **not** a wedge

With the backup gone for good, on the candidate:

```
plan     -> ok:true
verify   -> ok:true
recover  -> {"ok": true, "state": "none"}          # no journal to clear
install  -> InstallError(path unavailable: …/<digest>.json)
uninstall-> InstallError(cannot read …/<digest>.json)
```

`install` and `uninstall` keep refusing — correctly, because the pristine bytes they would have to write are
genuinely destroyed — but they refuse **idempotently and without leaving anything durable**, and `plan`, `verify`
and `recover` stay fully functional. Restoring the backup directory out of band immediately makes `uninstall`
succeed and return all four configs to byte-exact pristine with `latest-receipt.json` cleared. That is round 4's
semantics restored exactly, and it is the correct behaviour: nothing is *locked*, only *unrecoverable data* is
missing. Verified identically on 3.9.6 and 3.14.6.

### 1.3 Non-vacuity of the candidate's own new test

`test_uninstall_leaves_no_pending_journal_when_a_live_rows_backup_is_unloadable` run against the **baseline**
module fails at exactly the intended assertion:

```
AssertionError: True is not false          # tests/test_install_bridge.py:811 -> assertFalse(PENDING_PATH.exists())
```

Not a no-op test.

---

## 2. (b) R5-P2-A — **FIXED** on the indeterminate axis, verified with a stronger probe

### 2.1 The candidate's own test under-claims

The candidate test asserts, after the refusal, that the two discovered configs still have 2 handlers, and comments
"crucially, unlike round 5's bug, neither discovered config was rewritten either". **That assertion cannot
distinguish the two outcomes**: the test never bumps the release between the two installs, so `updated[path] ==
originals[path]` and `install()` would skip the write even without the fix — the handler count stays 2 either way.
The only assertion that actually discriminates is `assertFalse(PENDING_PATH.exists())`, which does fail on the
baseline (verified). So the test is non-vacuous, but weaker than its comment. (→ **R6-P3-B**.)

### 2.2 My strengthened probe (`probe_precheck.py`) — this one discriminates

Same shape, but with `bump_release()` between the two installs, so a rewrite is byte-visible:

| after the refused `install #2` | candidate | baseline |
|---|---|---|
| error | `cannot determine whether …/pool-bravo/home/hooks.json exists` | `install failed and the durable recovery journal remains pending` |
| pending journal | **False** | **True** |
| `latest-receipt.json` install_id | still **install #1's** | already **install #2's** |
| `.codex/hooks.json` | **byte-identical, still running the old release** | rewritten to the new release |
| `pool-alpha`, `pool-charlie` | **byte-identical, still running the old release** | rewritten to the new release |
| `verify` afterwards | `ok:true` | `pending install journal must be recovered first` |

This is precisely R5-P2-A's complaint ("reports install failed while every other account is in fact already
bridged") and it is gone. The pre-check sits at `install_bridge.py:888-889`, before `write_runtime()`, before the
backup writes, before the receipt and before the journal — the correct place.

### 2.3 The permanent-retirement path is not collateral damage

`scenario_b.py` B2: `shutil.rmtree()` of a whole account tree, then an **upgrade** install. Candidate:
`install` succeeds, the row is carried forward, `verify` → `ok:true` with the path in `unreachable`, `uninstall` →
`ok:true` with the path in `unreachable` and every surviving account byte-exact pristine. R3-P1-A intact.

`scenario_b.py` B6 is a bonus the fix earns for free: a carried-forward path that now resolves **off the SSD** is
rejected by the pre-check (`path is outside Extreme SSD`) before anything is written, instead of at commit time.

---

## 3. (c) Test suite — 72/72 real, on both interpreters

```
/usr/bin/python3 -m unittest discover -s tests -v      ->  Ran 72 tests ... OK   (3.9.6)
python3           -m unittest discover -s tests -v      ->  Ran 72 tests ... OK   (3.14.6)
```

* 33 `install_bridge` methods (was 31) + 39 `claude_memory_hook` methods = 72.
* Both new tests **fail on the round-5 baseline** at the intended assertion, and **pass standalone**.
* The whole `InstallEndToEndTests` + `InstallBridgeTests` + `UninstallCrashRecoveryTests` set also passes with the
  method order reversed → no inter-test coupling.
* `UninstallCrashRecoveryTests` is a genuine `SIGKILL` test (asserts `returncode == -SIGKILL`), not a simulated
  in-process exception. I reproduced its mixed-state → recovery result independently in `scenario_e.py` E1 with 4
  configs: after the kill, 3 accounts still bridged + `.codex` reverted + journal on disk, then
  `recover` → `{"state": "uninstalled"}` and journal gone.

**One behavioural divergence between the two interpreters** — see **R6-P2-B** in §5.2. It does not change any test
result, but it does change what the fixed code path is worth on the interpreter that will actually run.

---

## 4. (d) Rounds 1–5 regression sweep — clean (9/9)

| round | fix | independent check | result |
|---|---|---|---|
| 1 | P1-2 uninstall is durably journaled | real `SIGKILL` at `atomic_write` #3, 4 configs, then `recover` | **PASS** — mixed state → `{"state":"uninstalled"}`, journal cleared, all configs pristine |
| 1 | `owned_handler` structural `--bridge-id` match | 4 adversarial command strings (substring, shell comment, wrong adjacency, real form) | **PASS** — `False/False/False/True` |
| 1 / 4 | 5+2 guarded `stat()` sites | `_mode_bits(missing)`, `_path_is_absent(missing)`, `_path_is_absent(unreadable)` | **PASS** — `InstallError` / `True` / `InstallError`, never a bare `OSError` |
| 2 | R2-P1-A `prev_*` / `before_*` separation | upgrade install then rollback; `prev_backup` points at install #1's after-backup | **PASS** (also exercised implicitly by every upgrade scenario) |
| 2 | R2-P1-B lock creates `.shared-runtime` at `0o700` | fresh sandbox, `_acquire_exclusive_lock()` with no `.shared-runtime` | **PASS** — mode `0o700`, then `install` succeeds |
| 3 | R3-P1-A permanently retired account carried forward | `rmtree` of an account tree + upgrade + `verify` + `uninstall` | **PASS** — `unreachable`, no lockout |
| 3 / 4 | R3-P2-A / R4-P2-A pruned backup directory tolerated | carried-forward row + `rmtree` of the **old** backup dir | **PASS** — `verify` and `uninstall` both `ok:true`, no journal |
| 4 | R4-P1-A fail **closed** on an unreadable managed config | `chmod 0o000` on an account's `home/` after install | **PASS** — `verify` and `uninstall` both refuse with `cannot determine whether … exists`, **no journal**, handler untouched; after `chmod` back, `uninstall` completes normally |
| 5 | (both round-5 findings) | §1, §2 | **FIXED** |

---

## 5. (e) New / unreported problems

### 5.0 The three things the dispatch specifically asked me to look at

**① Do the two new eager-load dictionaries cover every row their loops consume?** **Yes, exactly.** Both
comprehensions filter `if row["state"] not in ("before", "both", "absent")` and both write loops skip on
`if row["state"] in ("before", "both", "absent")` — literal complements, and `row["state"]` is computed once in
`_receipt_rows()` and never mutated, so the two cannot diverge at runtime. No row is loaded-but-skipped or
skipped-but-consumed, and there is no `KeyError` path. I also fed `uninstall()` a hand-built receipt with **two
rows resolving to the same real file** (via a symlinked `home/`): the shared dict key does not produce a
`KeyError`, the uninstall completes, no journal is left. (The same construction does surface **R6-P3-C**, below.)

**② Does the new carried-forward pre-check introduce a TOCTOU problem?** It narrows an existing window rather than
opening a new one. Before the fix, the classification happened at commit time (after every write); now it happens
before the first write and the commit-time classification still runs. A state change *between* the two is still
detected — just at the old, bad point. I built that race explicitly (`scenario_b.py` B5: an account returns and
becomes unreadable inside `timestamp_id()`, i.e. after the pre-check and before the first durable write) and it
does end in a stuck journal — but it needs a precisely-timed external change, whereas R5-P2-A needed none. That is
a genuine improvement, and I do not count it as a new defect. What that experiment *did* surface is **R6-P2-C**.

**③ Did this round adopt the "write the preconditions down and assert them" approach opus asked for in round 5?**
**No.** The diff is again two point fixes plus two scenario-specific regression tests. There is no named invariant,
no shared helper, and no test that asserts the general property in the abstract. Concretely, the property both new
tests are instances of is: *"if `install()` or `uninstall()` raises, `PENDING_PATH` must not exist and no managed
config may differ from its pre-call bytes"* — that is a one-line assertion helper that could be applied to **every**
failure test in the class (there are already ≥8 of them), and it is exactly what would have caught R5-P1-A,
R5-P2-A, and **R6-P2-A** below in one go rather than one per round. The comments in this file are unusually good
history; they are not a substitute for an executable invariant.

---

### 5.1 R6-P1-A — **P1, blocking** — a renamed account path resurrects round 1's P1-1

**Reproduced on:** candidate `f8abefc9f0` **and** baseline `9e2205dc43`, on **both** 3.9.6 and 3.14.6.
**Pre-existing — not introduced by this round.** Not mentioned in any of the five previous review files
(I grepped all ten for `rename` / `previous_row is None` / `new path` / `already contains`; round 3 and round 5
discuss renames only in the *path disappears* direction, round 4's `g2` covers *re-provision at the same path with
fresh content*).

**Trigger.** A pooled account directory is **renamed or moved** between two installs, with its `hooks.json` content
untouched. No crash, no race, no privilege, no permission change, no mock.

```
install                                   # .codex + 3 pooled accounts, all bridged
mv codex-accounts/pool-alpha codex-accounts/pool-delta
install                                   # ok
verify                                    # ok:true, 4 configs
uninstall                                 # ok:true, "restored" lists pool-delta
```

**What happens.** At the second install, `discover_hook_configs()` returns the *new* path string, so
`previous_rows_by_path.get(os.fspath(path))` is `None` and `install()` takes this branch
(`install_bridge.py:915-925`):

```python
if previous_row is None:
    # Never touched by a previous install this receipt covers --
    # its current content genuinely is the pristine baseline, [...]
    baseline_raw[path] = raw
```

That comment is false here. `raw` is the *already-bridged* content from install #1. The receipt row written for
`pool-delta` is unmistakable:

```
row main          before==after? False  before_mode=0o644
row pool-bravo    before==after? False  before_mode=0o644
row pool-charlie  before==after? False  before_mode=0o644
row pool-delta    before==after? True   before_mode=0o600     <-- "pristine" == installed, at our own mode
row pool-alpha    before==after? False  before_mode=0o644     <-- old path, carried forward, correct baseline
```

and the backup file stored as its pristine baseline literally contains our handler
(`backup file is pristine? False  contains our handler? True`).

**Consequence.** `state` for that row is `both`, so `uninstall()`'s revert loop **skips it entirely** — while still
listing it in `restored`:

```
uninstall -> {"ok": true, "restored": [ …, ".../pool-delta/home/hooks.json"], "unreachable": [".../pool-alpha/…"]}

pool-bravo    handlers=1 owned=0 pristine=True
pool-charlie  handlers=1 owned=0 pristine=True
main(.codex)  handlers=1 owned=0 pristine=True
pool-delta    handlers=2 owned=1 pristine=False      <-- the bridge is still installed
```

And it is **live and undetectable**: after that `uninstall`,

```
release dir still present : True
hook script still present : True
policy still present      : True
-> the leftover handler is fully executable: True
true pristine baseline recoverable from any receipt: False      # latest-receipt.json was deleted
```

`verify` → `not installed: no receipt found`. `plan` → `ok:true`, happily listing `pool-delta` as installable.
No tool action anywhere in the chain reports the leftover, and the true pristine bytes are gone with the receipt.
This is the round-1 P1-1 outcome as `install()`'s own comment (`install_bridge.py:819-824`) describes it —
"uninstall would 'successfully' restore back to an already-installed state, every tool (verify, uninstall, recover,
plan) would report ok:true, and the bridge would keep running on every Codex prompt with no error anywhere in the
chain" — and it means Claude memory keeps being injected into that account's
Codex prompts after the operator has been told the bridge is gone.

**With a release upgrade it is the same, one step less obvious** (`scenario_c.py` C2): `before != after` there, so
the row is not `both` and `uninstall` *does* write — but it writes the **v1-bridged** bytes it recorded as
"pristine", so the account is restored to *still bridged*, just at the older release. Same end state.

**Blast radius is not limited to one account** (`scenario_d.py` D2): if the change is one level up — e.g.
`codex-accounts/` moved and symlinked back, an entirely ordinary way to relocate storage on an external SSD — then
**every** pooled account's path changes at once. The resulting receipt has 7 rows, 3 of them with an
already-bridged "pristine" baseline, `verify` returns `ok:true` while listing the same three files **twice each**,
and `uninstall` ends with `uninstall failed and the durable recovery journal remains pending`.

**Reachability on the real machine.** `codex-accounts/` holds UUID-named homes, and the pooled-account tooling
re-provisions under a *fresh* UUID — a genuinely new account arrives with a genuinely pristine `hooks.json`, which
this branch handles correctly. The bug needs a path change that *preserves* content: a `mv`/relocation of an
existing account directory, a restore-from-backup that lands the tree elsewhere with a symlink, or a symlinked
`codex-accounts`. None of those is routine, but none needs anything more than a single `mv`, and the existing,
blessed test `test_install_carries_forward_a_temporarily_undiscovered_config_without_losing_baseline` is one
keystroke away from it — it renames a managed path away and back; renaming it away and giving it a new permanent
name is this bug.

**Why P1 and not P2.** The three criteria this series has used throughout are met: (i) reproducible with no crash,
race, or privilege; (ii) silent — every tool reports success; (iii) irreversible — `latest-receipt.json` is deleted
in the same operation, so the true baseline is destroyed and no supported action can ever detect or undo the
leftover. It also lands on the one outcome this installer exists to prevent.

**Minimal fix.** `install()` already has everything it needs. In the `previous_row is None` branch, refuse to
adopt a baseline that is not actually pristine:

```python
if previous_row is None:
    payload = strict_json(raw)
    handlers = payload["hooks"]["UserPromptSubmit"]          # already shape-checked by update_hook_config above
    if any(owned_handler(handler) for handler in handlers):
        raise InstallError(
            "refusing to record an already-bridged config as a pristine baseline "
            f"(did this path move since the last install?): {path}"
        )
    baseline_raw[path] = raw
    ...
```

A clean refusal is the right outcome: the operator either restores the old path name (the carried-forward row then
does its job) or runs `uninstall` first. Recommended companion tests: `install → mv account → install` must refuse;
`install → mv account → mv back → install` must still succeed; and the general invariant from §5.0③ asserted over
every failing action.

---

### 5.2 R6-P2-A — R5-P2-A's fix only covers ENOENT-vs-indeterminate

`_path_is_absent()` answers exactly one question — "is this path gone, or can't we tell?" — and the new pre-check
(`install_bridge.py:888-889`) discards its return value, using it purely for its raising side effect. A
carried-forward path that **exists but is not a usable managed file** therefore sails through the pre-check and
produces the *identical* wedge R5-P2-A described.

`scenario_b.py` B3 — `hooks.json` replaced by a directory (so `Path.is_file()` is `False` → undiscovered, but
`lstat()` succeeds → the pre-check passes), then an upgrade install:

```
install#2 -> InstallError(install failed and the durable recovery journal remains pending)
pending=True   latest_receipt=True
main, pool-alpha, pool-charlie: handlers=2 owned=1     <-- all bridged with the new release
plan    -> ok:false
verify  -> pending install journal must be recovered first
recover -> InstallError(unsafe file ownership or mode: .../pool-bravo/home/hooks.json)
uninstall -> same
```

`scenario_b.py` B4 — an account directory turned into a **symlink** (`discover_hook_configs()` skips symlinked
account dirs by design at `install_bridge.py:236`), plus an upgrade: same stuck journal, this time via
`committed install journal has config drift`, with all four configs left bridged. Identical on both interpreters.

The pre-check has all the information required to catch this: it already resolves the path, and the commit-time
check that *does* catch it is `_receipt_rows()` → `validate_owned_file()`. Running the carried-forward rows through
the same classification the commit will use (i.e. `_receipt_rows()` over `carried_forward_rows`, or at minimum an
`lstat` + `S_ISREG` + ownership/mode check) would close the whole axis instead of one point on it. **P2**, matching
the severity opus assigned R5-P2-A itself — nothing is silently wrong on disk, the wedge is loud, and removing the
offending object un-wedges it.

---

### 5.3 R6-P2-B — on `/usr/bin/python3` 3.9 the fixed path is unreachable, and `plan`/`install` crash

This is R5-P3-A, deliberately deferred — but the interpreter matrix makes it sharper than "a bare exception on one
input", and it partially undercuts fix ②'s value:

| `chmod 0o000` on an account's `home/`, then… | `/usr/bin/python3` 3.9.6 | `python3` 3.14.6 |
|---|---|---|
| `Path.is_file()` on the unreadable parent | **raises `PermissionError`** | returns `False` (swallows EACCES) |
| `main("plan")` | **UNCAUGHT `PermissionError`** → raw traceback, no JSON | `rc=0` (and silently reports only 3 of 4 configs) |
| `main("install")` | **UNCAUGHT `PermissionError`** → raw traceback, no JSON | `rc=1`, clean `{"ok": false, …}` **via the new pre-check** |
| `main("verify")` / `main("uninstall")` | `rc=1`, clean | `rc=1`, clean |
| `main("recover")` | `rc=0` | `rc=0` |

So on the interpreter that is baked into the generated hook command and used to run the suite, the exact input
R5-P2-A was reported against never reaches the new pre-check at all — it dies earlier in
`discover_hook_configs()`, escaping `main()`'s `except InstallError` and violating the
`{"ok": false, "error": …}` contract that five rounds of work have established everywhere else in this file
(`resolve_ssd_path`, `_mode_bits`, `atomic_write`, `_acquire_exclusive_lock`, `remove_file_durable`,
`_path_is_absent`, …). The candidate's own test comment is honest about this, and mocks `discover_hook_configs()`
precisely because of it — so the new test proves the *fixed code path* works, not that the *reported scenario* is
fixed on 3.9.

Nothing durable is written and nothing is lost, so this stays **P2**. The fix is two lines — the same
`except OSError` → `InstallError` treatment `_path_is_absent()` already implements, applied to the
`candidate.is_file()` call at `install_bridge.py:239` (and it would then also make the *silent drop* on 3.14 a loud
refusal, which is what the round-5 report actually asked for).

---

### 5.4 R6-P2-C — the install-direction rollback also reverts rows the transaction never touched

`recover_pending_install()`'s `kind == "install"` rollback loop (`install_bridge.py:760-768`) uses
`install_state == "after"` as its proxy for "this transaction wrote this row". For a **carried-forward** row that
proxy is wrong: such a row's `after_*` is the *previous* transaction's after state, which is exactly what is on
disk and exactly what should stay there — the interrupted install never touched it.

`probe_b5.py` (real, unmocked, one interrupted install with one carried-forward row):

```
row .codex/hooks.json                          state=after  install_state=after
row codex-accounts/pool-alpha/…                state=after  install_state=after
row codex-accounts/pool-bravo-stashed/…        state=absent install_state=absent
row codex-accounts/pool-charlie/…              state=drift  install_state=prev
row codex-accounts/pool-bravo/…                state=after  install_state=after   <-- carried forward, untouched

recover -> {"ok": true, "state": "rolled_back"}

pool-bravo   owned=0  mode=0o644     <-- silently un-bridged and left at the pristine mode
pool-alpha   owned=1  mode=0o600
verify -> InstallError(file is not private: .../pool-bravo/home/hooks.json)
```

The rollback reverted `pool-bravo` — which this install never wrote — to *pristine at mode `0o644`*, while
`latest-receipt.json` still says it is installed at `0o600`. `verify()` then fails permanently (on the mode check,
before it even gets to the digest), and for an upgrade chain the same loop would silently walk a carried-forward
account **back one release**. It fails in the safe direction (the bridge is removed, not left running) and
`uninstall` still works afterwards, so **P2** — but it is a real, unreported correctness gap in the same
carried-forward machinery this round is patching, and the fix is to exclude rows that were not in this
transaction's write set (the receipt already distinguishes them: a carried-forward row is precisely one whose
`prev_backup` is not under this `install_id`'s backup directory, or it could simply be marked).

---

### 5.5 P3 notes

* **R6-P3-A — the new pattern is applied asymmetrically.** Fix ① added the eager load to `recover`'s
  *uninstall*-direction loop with the rationale "a re-run always starts from the same, fully-untouched state
  instead of an unpredictable partial one". That rationale applies verbatim to the *install*-direction rollback
  loop 20 lines above, which still calls `_load_backup(row["prev_backup_obj"], …)` inside the loop. `scenario_e.py`
  E2 (real `SIGKILL` mid-upgrade, then the previous install's backup directory destroyed) shows the equivalent
  outcome there: journal stuck, `plan` `ok:false`, `verify`/`install`/`uninstall`/`recover` all refusing
  permanently. Both crash-plus-data-loss cases are inherent (the bytes are gone), and the candidate's own comment
  says so honestly for the uninstall side — but the loop should be made all-or-nothing on both sides for the same
  reason, or neither.
* **R6-P3-B** — §2.1: the second new test's headline claim is not established by its assertions; add a
  `bump_release()` and assert the discovered configs still carry the **old** command.
* **R6-P3-C** — when several receipt rows resolve to one file, `verify()` returns `ok:true` with the same path
  listed once per row (`scenario_d.py` D2 shows three files reported six times), so "all N accounts verified" and
  "one account verified N times" are indistinguishable in the tool's own output. `_validate_receipt_shape()` does
  not reject duplicate resolved paths.
* R5-P3-B (`backup` lost the same-device check) and R5-P3-C (`ENOTDIR` → indeterminate) re-confirmed as
  non-blocking; the digest check in `_load_backup()` still catches real content substitution, and B6 shows the
  off-SSD escape path now refuses *earlier* than before.

---

## 6. What I checked and did **not** find

To be explicit about the negative results, since this file's history makes "I found nothing there" worth stating:

* No divergence between the two eager-load dictionaries and their write loops (structural + empirical).
* No `KeyError`, no lost row, no re-read of an already-cached backup in either loop.
* No case where the eager load itself writes, mutates, or locks anything.
* No memory or size hazard from caching backups (`MAX_MANAGED_FILE_BYTES` is 4 MiB and only non-skipped rows load).
* No regression in any of the nine rounds-1–5 fixes I re-tested.
* No new failure introduced by the pre-check's *position* (it runs before `write_runtime()`, so a refusal cannot
  even leave a new release directory behind — confirmed).
* No test in the suite that passes for the wrong reason, other than the discrimination gap in §2.1.

---

## 7. Bottom line

The round-6 diff does what it says: **R5-P1-A and R5-P2-A are really fixed**, the two new tests are real, the
suite is really 72/72 on both interpreters, and — for the first time in six rounds — **the fix for the previous
round's finding did not introduce a new defect of its own**. That is a genuine change in trajectory and it should
be recorded as such.

The blocker is older than this round: `install()` will adopt an already-bridged config as its own pristine
baseline whenever a managed path's *name* changes while its *content* does not, and the resulting `uninstall`
reports success while leaving the bridge executing and destroying the only record that could reveal it. One `mv`
reproduces it, on every revision I tested, on both interpreters. Until that is closed — and, ideally, until the
general "a failed action leaves no journal and no modified config" invariant from §5.0③ is written down and
asserted rather than rediscovered — this candidate must not be installed on the real machine.

**Verdict: NO-GO.** One reproducible P1 (R6-P1-A, pre-existing), three P2s (R6-P2-A/B/C), three P3s.
