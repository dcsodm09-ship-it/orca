# Independent read-only review — `install_bridge.py`, round 8

**Reviewer:** Claude `opus5` / effort `max`, independent, no knowledge of the parallel round-8 review.
**Candidate:** `37d2433a77` — *fix(install_bridge): make uninstall refuse an untracked owned handler instead of a permanent lockout*
**Baseline:** `8fe75b209e` (round-7 review baseline)
**Scope:** `claude-codex-memory-bridge/install_bridge.py`, `claude-codex-memory-bridge/tests/test_install_bridge.py`
**Date:** 2026-08-17
**Method:** every reproduction below ran in a private, throwaway fake-SSD tree under the session scratchpad, in
separate processes, on both `/usr/bin/python3` 3.9.6 and PATH `python3` 3.14.6. The real
`/Volumes/Extreme SSD` Codex configuration was never read for install purposes and never written.

---

## Verdict: **NO-GO**

The round-8 change does close the exact door round 7 reported: calling `uninstall()` directly while a managed
account directory has been renamed **to a sibling name under `codex-accounts/`** now refuses cleanly, keeps the
receipt byte-exact, writes no journal, touches no config, and leaves the "rename it back" remedy intact. I
verified that independently, with my own fixture names, on three variants. (a), (b) and (d) all pass.

But the safety scan was scoped to one shape of the problem, and it is placed one call too late. The harm
round 7 described — `uninstall` completing, the receipt being deleted, a live bridge handler surviving
untracked, and the tool then refusing every recovery action forever — is **still reproducible on the candidate,
through four different doors**, one of them using nothing but a real `SIGKILL` (the file's own stated threat
model) and a plain `mv`. In addition the new scan block itself introduces a bare-`AttributeError` crash and two
new refuse-forever paths on inputs the baseline handled correctly.

| ID | Finding | Severity | Novelty |
|----|---------|----------|---------|
| **R8-P1-A** | `recover_pending_install()` has **no** untracked-owned-handler scan, and `uninstall()` calls it *before* the new scan. Real `SIGKILL` mid-uninstall + a plain `mv` of an account dir + one ordinary `uninstall` ⇒ receipt deleted, handler live and untracked, **permanent lockout**; renaming back does **not** help. This is R7-P1-A verbatim, on the candidate. | **P1, blocking** | R7-P1-A, incompletely fixed |
| **R8-P1-B** | The scan only sees `codex-accounts/<one-level>/home/hooks.json`. Moving the account dir into a *subdirectory*, *out of* `codex-accounts`, or renaming its inner `home/` ⇒ `uninstall` reports `ok:true`, deletes the receipt, leaves the handler live — then **permanent lockout**. No crash, no race, no privilege. | **P1, blocking** | R7-P1-B, incompletely fixed |
| **R8-P1-C** | `uninstall()` now depends on the full enumeration succeeding. Deleting `~/.codex/hooks.json`, or renaming `~/.codex` or `codex-accounts/`, makes `uninstall` (and `install`, `plan`) refuse with `path unavailable` **forever**, while `verify` still reports `ok:true` and every remaining config keeps executing the bridge. The baseline uninstalled these inputs correctly. | **P1, blocking** | **new this round** (regression) |
| **R8-P1-D** | The new scan block re-implements the `payload.get("hooks", {}).get(...)` idiom **without** `update_hook_config()`'s structure check, against arbitrary untracked content. An unrelated account whose `hooks.json` is `{"hooks": []}` / `{"hooks": null}` / `{"hooks": "x"}` makes `uninstall` die with a **bare `AttributeError` traceback and zero stdout** — the exact failure-contract violation this round's own `is_file()` guard exists to prevent, 20 lines below that guard. | **P1, blocking** (non-destructive) | **new this round** (regression) |
| **R8-P2-A** | `strict_json()` sits **outside** the scan's `except InstallError: continue`, so any untracked account with malformed JSON blocks `uninstall` entirely, with a message that names no path (`invalid hook JSON`). Baseline uninstalled fine. | P2 | **new this round** (regression) |
| **R8-P2-B** | The R5-P3-A / R3-P3-B / R6-P2-B fix is **half** a fix. Only the 3.9 *crash* is closed; on 3.13+ `Path.is_file()` swallows `EACCES` unconditionally, so the new guard is dead code and the scan **fails open**. On 3.14 a renamed-and-permission-restricted account dir ⇒ `uninstall` `ok:true`, receipt deleted, handler live. The commit's "彻底修好了" claim is not accurate. | P2 | **new this round** (claim overstated) |
| **R8-P3-A** | The refusal message misdiagnoses every non-rename trigger (a cloned account dir, a `*.bak` snapshot kept beside the original) as "a managed account directory may have moved — restore it to its receipt-recorded path", and in that state **both** `install` and `uninstall` refuse, with no supported remedy short of hand-editing. | P3 | **new this round** |

Confirmed clean: (a) sibling-rename refusal, (b) no permanent lockout **on the sibling-rename path**, (c)
`is_file()` guard on 3.9, (d) no false positives on the three named benign scenarios, (e) 74/74 on both
interpreters, (f) no regression in any round 1–6 fix I could exercise, and the new test is non-vacuous.

This is the **eighth consecutive round** in which the round's own fix introduces or leaves a reproducible P1.
Two of this round's four P1s are the *previous* round's finding surviving in a slightly different shape; two are
brand-new regressions authored inside the new 24-line block itself.

---

## 1. What the change does

Three edits (`git diff 8fe75b209e 37d2433a77 -- claude-codex-memory-bridge/`, +175/−22):

1. **`discover_hook_configs()` split** into `_enumerate_hook_configs()` (raw enumeration, `install_bridge.py:220`)
   + a thin `discover_hook_configs()` (`:270`) that adds the `len(unique) < 2` install-time policy. Correct
   split; the safety scan genuinely must not inherit the `>= 2` minimum.
2. **`candidate.is_file()` guarded** (`:261-264`) → `InstallError` on `OSError`.
3. **`uninstall()` safety scan** (`:1242-1265`), after `recover_pending_install()` and `read_receipt()`, before
   `_receipt_rows()`: for every enumerated config not in `{row["path"] for row in receipt["configs"]}`, read it
   and refuse the whole uninstall if it holds an `owned_handler()`.
4. **`install()`'s R6-P1-A message** reworded to lead with "restore it to its receipt-recorded path".

---

## 2. (a) + (b) — the reported R7-P1-A / R7-P1-B path is fixed

My own fixture (`pool-alpha`, `pool-beta`, `pool-gamma`; nothing reused from the candidate suite).

**A1 — install, `mv codex-accounts/pool-beta codex-accounts/pool-beta-2026relocated`, then `uninstall()` directly:**

```
[uninstall(renamed)] InstallError -> refusing uninstall: found an owned hook handler at a path the
  current receipt does not track (a managed account directory may have moved -- restore it to its
  receipt-recorded path before uninstalling): .../codex-accounts/pool-beta-2026relocated/home/hooks.json
    receipt unchanged (byte-exact): True
    receipt still exists:           True
    pending journal absent:         True
    relocated config untouched:     True   (owned handlers still 1, content byte-identical)
    pool-alpha untouched:           True
    main config untouched:          True
```

Then the escape hatch:

```
[uninstall(restored path)] ok:true, restored 3 configs
    main / pool-alpha / pool-beta: owned handlers now 0; content == pristine  True
    receipt removed after real uninstall: True
```

**A2 — clone variant** (`cp -a pool-beta pool-beta-clone` while installed): also refused, receipt unchanged.
**A3 — drift-first variant** (rename `pool-beta` away, re-provision a *fresh* pristine account at the old path):
the untracked scan runs before the drift check, so it wins and refuses on the archived copy; the fresh account
is untouched.
**B1 — follow the old advice end to end** (rename → `uninstall` → `install` → rename back → `uninstall` →
`install`): every step either refuses cleanly or succeeds; the receipt survives every refusal; the final
`uninstall` restores all three configs to pristine and a fresh `install` then works. **No permanent lockout on
this path.**

Non-vacuity of the candidate's own new test, checked independently: round-8 tests against round-7 code →
`FAIL: test_uninstall_refuses_when_a_discovered_config_carries_an_untracked_owned_handler`,
`AssertionError: InstallError not raised`. Claim confirmed.

---

## 3. R8-P1-A — `recover_pending_install()` bypasses the scan; **permanent lockout**, reproduced with a real `SIGKILL`

`uninstall()` calls `recover_pending_install()` at `install_bridge.py:1212`. The new scan is at `:1242`. A
pending *uninstall* journal therefore commits — including `remove_file_durable(latest-receipt.json)` at
`:832` — **before the scan ever runs**. `recover_pending_install()` has no scan of its own, and it is also a
first-class CLI action (`main()` `recover`) and the first thing `install()` does (`:838`).

Methodology copied from the candidate suite's own `UninstallCrashRecoveryTests`: a genuine `SIGKILL` in a child
process at `atomic_write` call #3, not a simulated exception.

```
[install] ok
child returncode: -9   (real SIGKILL)
  pending journal survived: True
  receipt still present:    True
  main owned handlers now:  0        <- reverted before the kill
  pool-alpha owned:         1        <- still bridged
  pool-beta  owned:         1        <- still bridged

-- operator now relocates an account dir the interrupted uninstall had not reached --
   mv codex-accounts/pool-beta codex-accounts/pool-beta-relocated

-- operator runs the obvious next command --
[uninstall (2nd attempt)] InstallError -> not installed: no receipt found
    receipt now deleted:                              True
    pending journal cleared:                          True
    RELOCATED config still carries owned handler:     1     <-- live bridge, no record anywhere

-- is there any way back? --
[verify]          InstallError -> not installed: no receipt found
[uninstall again] InstallError -> not installed: no receipt found
[recover]         ok:true, state "none"          (no-op)
[install]         InstallError -> refusing to record an already-bridged config as a pristine baseline ...
[plan]            ok:true                        (no effect)

-- the documented "always-safe" remedy: put the directory back --
[uninstall after restoring path] InstallError -> not installed: no receipt found
[install   after restoring path] InstallError -> refusing to record an already-bridged config as a
                                  pristine baseline ... codex-accounts/pool-beta/home/hooks.json
[verify    after restoring path] InstallError -> not installed: no receipt found
    pool-beta STILL bridged: 1
    pool-beta content == pristine: False
```

This is **opus's R7-P1-A, verbatim, on the round-8 candidate**: every action refused forever except `plan` and a
no-op `recover`, a live handler executing on every Codex prompt, no receipt to reveal it, and — precisely
contradicting `uninstall()`'s new comment at `:1239-1241` ("the always-safe remediation — restore the path to
where the receipt expects it — remains available") — **restoring the path does not help**, because the receipt
is already gone. Only hand-editing `hooks.json` recovers.

The trigger set is exactly the file's own stated threat model plus one `mv`: "a real SIGKILL can end the process
at any point" (`:721`) and "an ordinary way to relocate storage on an external SSD — a plain `mv`, no
crash/race/privilege/mock" (`:949-951`). Nothing here is exotic.

The same bypass is reachable without a crash by invoking `install_bridge.py recover` directly with a pending
uninstall journal present; I confirmed that separately (`recover` → `{"ok": true, "state": "uninstalled"}`,
receipt gone, moved config still bridged, then the identical lockout).

---

## 4. R8-P1-B — the scan's search shape is narrower than the harm it guards

`_enumerate_hook_configs()` only ever looks at `~/.codex/hooks.json` and `codex-accounts/<dir>/home/hooks.json`
for `<dir>` **one level deep**. Three moves that leave the handler live are invisible to it:

| move | `uninstall()` | receipt | moved config |
|---|---|---|---|
| `mv codex-accounts/pool-beta codex-accounts/archive-2026/pool-beta` | **`ok:true`** | **deleted** | **still bridged (1)** |
| `mv codex-accounts/pool-beta local-homes/retired/pool-beta` | **`ok:true`** | **deleted** | **still bridged (1)** |
| `mv codex-accounts/pool-beta/home codex-accounts/pool-beta/home-2026` | **`ok:true`** | **deleted** | **still bridged (1)** |

In each case the old path is classified `absent` and reported in `unreachable`, which reads as a normal,
successful, R3-P1-A-sanctioned outcome. Follow-on, for the nested-move case:

```
[uninstall (nested move)] ok:true  unreachable=[.../pool-beta/home/hooks.json]
    receipt deleted: True ; moved config still bridged: 1
-- move it back to exactly where the receipt expected it --
[uninstall after restoring path] InstallError -> not installed: no receipt found
[install   after restoring path] InstallError -> refusing to record an already-bridged config as a pristine baseline ...
[verify    after restoring path] InstallError -> not installed: no receipt found
[recover]  ok:true state "none"   [plan] ok:true
    pool-beta STILL bridged: 1
```

Identical permanent lockout, reached with **no crash, no race, no privilege, no mock** — one `mv` into a
subfolder and one `uninstall`. Moving a pooled account into an `archive/` folder, or out of `codex-accounts` to
a different spot on the same SSD, is at least as ordinary as the sibling rename round 8 does handle; the fix
covers the one variant the round-7 reports happened to demonstrate rather than the class.

*(Checked and clean: replacing the account dir with a **symlink** to the relocated real dir is correctly caught
— `lstat()` skips the symlink, the real dir enumerates, the resolved path is not in the receipt, refusal.)*

---

## 5. R8-P1-C — `uninstall()` now inherits install-time existence preconditions (regression)

`_enumerate_hook_configs()` opens with three `resolve_ssd_path(..., must_exist=True)` calls (`:229`, `:230`,
`:233`, `:234`). Before this commit `uninstall()` never called any of them; a missing managed path was simply an
`absent` row, reported under `unreachable`, and the uninstall completed. Now any of them failing aborts the
whole uninstall.

Candidate `37d2433a77`, after a successful install, then `rm ~/.codex/hooks.json`:

```
[uninstall] InstallError -> path unavailable: .../local-homes/.codex/hooks.json
[install]   InstallError -> path unavailable: .../local-homes/.codex/hooks.json
[plan]      InstallError -> path unavailable: .../local-homes/.codex/hooks.json
[verify]    ok:true   unreachable=[.../.codex/hooks.json]     <-- reports the system as healthy
[recover]   ok:true   state "none"
    pool-alpha still bridged: 1
    pool-beta  still bridged: 1
```

Baseline `8fe75b209e`, same input:

```
baseline uninstall -> ok
  pool-alpha owned after: 0
  pool-beta  owned after: 0
  receipt gone: True
```

Same result for `mv ~/.codex ~/.codex-old` (`path unavailable: .../.codex`) and for
`mv codex-accounts codex-accounts-archived` (`path unavailable: .../codex-accounts`) — note the latter is
round 6's own "上级目录整体改名" variant, now unfixable from the uninstall side.

Escapability: only by luck. Re-creating `~/.codex/hooks.json` with content that is *not* byte-and-mode identical
to the recorded pristine baseline yields `refusing uninstall because a config changed`. Re-creating it with
exactly the pristine bytes works — but the operator has no way to learn those bytes from the tool (`verify`
prints digests, never content). In practice this is a refuse-forever state in which `verify` says `ok:true`.

This is the same lockout signature R3-P1-A was written to eliminate ("every action refused forever except
`plan`"), reintroduced for a different trigger by this round's own change.

---

## 6. R8-P1-D — bare `AttributeError` out of `uninstall()` (regression)

`install_bridge.py:1252-1257`:

```python
candidate_payload = strict_json(candidate_raw)
candidate_handlers = (
    candidate_payload.get("hooks", {}).get("UserPromptSubmit", [])
    if isinstance(candidate_payload, dict)
    else []
)
```

The `isinstance(..., dict)` guard covers the *top level* only. `install()` (`:985-990`) and `verify()` (`:1185`)
use the same idiom safely because both are preceded by `update_hook_config()` / an `after_sha256` match, which
guarantee `payload["hooks"]` is a dict. The new scan has **no** such precondition — it runs against arbitrary
content in an account this tool has never touched.

Round-8 candidate, one extra untracked account present:

```
[uninstall w/ {"hooks": []}]    CRASH -> AttributeError: 'list' object has no attribute 'get'
[uninstall w/ {"hooks": null}]  CRASH -> AttributeError: 'NoneType' object has no attribute 'get'
[uninstall w/ {"hooks": "x"}]   CRASH -> AttributeError: 'str' object has no attribute 'get'
[uninstall w/ []]               ok      (top-level guard catches this one)
[uninstall w/ {"other": 1}]     ok
```

At the CLI contract level:

```
main()  RAISED AttributeError: 'list' object has no attribute 'get'
stdout emitted before the crash: ''     <-- no {"ok": false, "error": ...} at all
```

`main()` catches only `InstallError` (`:1448`), so this is a raw Python traceback on stderr and an empty stdout —
the precise contract violation the *same commit's* `is_file()` guard comment invokes as its justification
("crash a plain `uninstall()` with a raw traceback instead of the clean `InstallError` this file's failure
contract requires everywhere else", `:256-258`), reintroduced 20 lines further down in the block that guard was
added for. Baseline `8fe75b209e` on the same input: `uninstall -> ok`.

Harm is blocking rather than destructive (the receipt survives; nothing is written), but it makes `uninstall`
impossible until the operator locates and edits a file the tool never names, and it breaks any JSON-consuming
wrapper.

---

## 7. R8-P2-A — `strict_json()` outside the skip guard (regression)

```python
try:
    candidate_raw = validate_owned_file(candidate)
except InstallError:
    continue                     # <- intent: "can't validate it, skip it"
candidate_payload = strict_json(candidate_raw)   # <- outside the guard
```

`validate_owned_file()` performs no JSON validation, so any untracked account with a non-JSON `hooks.json`
raises past the `continue`:

```
[uninstall w/ trailing comma] InstallError -> invalid hook JSON
[uninstall w/ plain text]     InstallError -> invalid hook JSON
[uninstall w/ empty file]     InstallError -> invalid hook JSON
[uninstall w/ duplicate keys] InstallError -> duplicate JSON key: hooks
```

Baseline: `uninstall -> ok` for all four. The error names no path, so the operator cannot tell which of N
accounts is at fault. Fail-closed is defensible in principle here, but the surrounding code's own intent
(`except InstallError: continue`) says otherwise, and the message makes it undiagnosable.

---

## 8. R8-P2-B — R5-P3-A / R3-P3-B / R6-P2-B are only half fixed

The guard added at `:261-264` closes the 3.9 crash. It does **not** close the fail-open half, and on 3.13+ it is
dead code:

```
# /usr/bin/python3 3.9.6
Path.is_file() on an un-enterable dir -> RAISED PermissionError [Errno 13]

# PATH python3 3.14.6
Path.is_file() on an un-enterable dir -> returned False (swallowed)
```

3.14's `Path.is_file()` delegates to `os.path.isfile()`, which swallows every `OSError`. So on 3.13+ the safety
scan silently drops any account it cannot stat — the original R5-P3-A defect ("silently drop an account from
discovery on ANY `stat()` failure it treats as 'not found'") — and the new scan inherits it directly:

```
# 3.14.6: install, mv pool-beta -> pool-beta-moved, chmod 0o600 pool-beta-moved
[uninstall] ok:true   unreachable=[.../pool-beta/home/hooks.json]
    receipt deleted: True
    moved config still bridged: 1

# 3.9.6, identical input
[uninstall] InstallError -> cannot determine whether .../pool-beta-moved/home/hooks.json is a managed config
    receipt deleted: False ; moved config still bridged: 1
```

The documented invocation is `/usr/bin/python3` (README lines 133-154), where behaviour is fail-closed, which
bounds the practical exposure — but nothing in the module enforces that, the suite is run on both, and the
commit's claim that this "顺带也把 R5-P3-A/R3-P3-B/R6-P2-B 彻底修好了 … 所有调用方都受益" is not accurate for
3.13+. It should be recorded as half-fixed, not closed.

---

## 9. R8-P3-A — the refusal message misdiagnoses non-rename triggers

Any untracked discovered config carrying an owned handler triggers the same message. Two benign, non-rename
cases hit it:

- **cloning a pooled account** (`cp -a pool-beta pool-beta-clone` while installed);
- **keeping a snapshot beside the original** (`pool-beta.bak/`) — a sibling under `codex-accounts`, so it is
  enumerated.

In that state `uninstall` refuses with "a managed account directory may have moved — restore it to its
receipt-recorded path" (nothing moved), and `install` refuses too with the R6-P1-A message. `verify` reports
`ok:true`. There is no supported action; deleting the clone or hand-editing it is the only way out (confirmed:
`rm -rf` the clone → `uninstall` → `ok:true`). Refusing is the right *decision*; the diagnosis and the absence of
any suggested remedy for this shape are the problem.

---

## 10. (d) — no false positives on the three named benign scenarios

| scenario | result |
|---|---|
| temporary rename **and rename back** (round-3 path) | `uninstall` `ok:true`, all three configs byte-exact pristine |
| add a brand-new pristine account after install | `uninstall` `ok:true`, new account untouched, receipt cleared |
| plain `install` → `uninstall` → `verify` → `uninstall` | `ok:true` / `not installed: no receipt found` / `not installed: no receipt found` — unchanged |
| 120 accounts | scan adds nothing measurable: install 0.17 s, uninstall 0.12 s |
| 3 MB untracked config present | uninstall 0.01 s |

Performance and timeout characteristics of the scan are fine; `validate_owned_file()`'s existing 4 MB cap bounds
the read.

---

## 11. (f) — rounds 1–6 spot-checks, all clean

Full suite: **74/74 OK** on `/usr/bin/python3` 3.9.6 (0.655 s) and **74/74 OK** on PATH `python3` 3.14.6
(0.503 s). 35 `install_bridge` methods + 39 `claude_memory_hook` methods = 74. Independent manual checks beyond
the suite:

| fix | check | result |
|---|---|---|
| P1-2 uninstall journal | real-SIGKILL child, journal survives, `recover` finalizes | ✅ (and see R8-P1-A for the *new* problem this exposes) |
| `owned_handler` precision / P3-1 | 4 near-miss commands incl. shell comment | ✅ all 4 correct |
| R2-P1-A `prev_*` / `before_*` | upgrade install → `before_sha256 != prev_sha256`, uninstall → pristine | ✅ |
| R2-P1-B lock dir creation | `RUNTIME_BASE` and parent both `0o700`, lock file `0o600` | ✅ |
| R3-P1-A retired account | `rmtree` an account → `verify` unreachable, re-`install` ok, `uninstall` ok+unreachable | ✅ |
| R3-P2-A / R4-P2-A lazy backups | retire an account → carried-forward row still points at the *first* transaction's backup dir → `rm -rf` that whole dir → `verify` ok+unreachable, `uninstall` ok, remaining configs pristine | ✅ |
| R4-P1-A fail-closed | `chmod 0o000` on a managed home → clean `InstallError`, nothing written | ✅ (message now comes from the new `is_file()` guard on 3.9; still `InstallError`) |
| R5-P1-A eager backup load | `rm -rf` backups dir → refuses before any write, **no** pending journal, all configs byte-exact | ✅ |
| R5-P2-A early carried-forward check | covered by suite + R4-P1-A probe | ✅ |
| R6-P1-A install-side refusal | rename + `install` → refuses, receipt intact | ✅ |

`plan()` and `install()` strictly *improved* on 3.9: a permission-restricted account directory that previously
bare-crashed them now yields a clean `InstallError` (verified pre-install on a fresh tree too). That part of the
round-8 change is good.

---

## 12. Reproduction index

All probes are self-contained and live in the session scratchpad
(`.../4e09674a-.../scratchpad/`): `harness.py` (isolated fake SSD), `probe_a.py` (a/b/d), `probe_c.py` (c),
`probe_g.py` (g), `probe_sigkill.py` (R8-P1-A), `probe_lockout.py` (R8-P1-B + baseline diffs),
`probe_final.py` (R8-P1-C escape analysis, R8-P1-D CLI contract, R8-P2-B, f), `probe_r3p2a.py` (targeted
R3-P2-A/R4-P2-A pruned-carried-forward-backup check). They monkeypatch
`SSD_ROOT`/`LOCAL_HOMES_ROOT`/`RUNTIME_BASE`/`PENDING_PATH`/`SOURCE_SCRIPT`/`volume_uuid`/`Path.home` exactly as
the candidate suite does, and never reference the real SSD paths.

---

## 13. What would have to change for GO

1. Move the untracked-owned-handler scan **into `recover_pending_install()`** (or run it before
   `recover_pending_install()` in `uninstall()` *and* inside `recover`), so no journal commit can delete the
   receipt while an untracked owned handler is reachable. Without this, R8-P1-A stands.
2. Make the scan's search **at least as wide as the harm**: recurse under `local-homes` for `hooks.json` files
   (bounded), or — simpler and strictly better — refuse the *receipt-deleting commit* whenever any receipt row
   is `absent` and its recorded content was `after`-state, rather than trying to find where it went. An `absent`
   row that was still bridged at the last known state is the actual signal; a directory search will always be
   one `mv` behind.
3. Do not make `uninstall()` depend on `resolve_ssd_path(..., must_exist=True)` for the codex home / accounts
   root. The scan should degrade to "found nothing" for a missing root, not abort the transaction — and if the
   project prefers fail-closed there, it needs a documented, tool-supported way out, which today does not exist.
4. Route the scan's payload inspection through `update_hook_config()`'s structure check (or add
   `isinstance(payload.get("hooks"), dict)`), and move `strict_json()` inside the `except InstallError: continue`.
5. Record R5-P3-A / R3-P3-B / R6-P2-B as **half fixed** (crash closed, silent-drop open on 3.13+), or close the
   fail-open half with an explicit `os.stat()` + errno discrimination in the style of `_path_is_absent()`.
6. Reword the refusal to cover the non-rename shapes and name a remedy for them.

Items 1–4 are blocking. Items 5–6 are not, but item 5's current claim is inaccurate as written.
