# Independent read-only review — `install_bridge.py`, round 7

**Reviewer:** Claude `opus5` / effort `max`, independent, no knowledge of the parallel round-7 review.
**Candidate:** `8fe75b209e` — *fix(install_bridge): refuse to adopt an already-bridged config as pristine after a rename*
**Baseline:** `f8abefc9f0` (round-6 review baseline)
**Scope:** `claude-codex-memory-bridge/install_bridge.py`, `claude-codex-memory-bridge/tests/test_install_bridge.py`
**Date:** 2026-08-17

---

## Verdict: **NO-GO**

The round-7 change does close the *silent-adoption* half of R6-P1-A, cleanly and non-vacuously — verified
independently on three rename variants. But it closes it by refusing, and the state that refusal leaves behind is
one **no tool action can ever leave**, while a live bridge handler keeps executing. The candidate's own error
message directs the operator straight into that state.

| ID | Finding | Severity | Novelty |
|----|---------|----------|---------|
| **R7-P1-A** | Following the new error message's own advice (`run uninstall first`) produces a **permanent lockout**: `install`/`verify`/`uninstall` refuse forever, only `plan`/`recover` still report `ok:true`, and a bridge handler stays live with the release runtime retained. No supported action recovers; renaming back does not help. | **P1, blocking** | **new this round** |
| **R7-P1-B** | R6-P1-A is only **half fixed**. Its stated harm — `uninstall` reporting `ok:true` while leaving a live, unrecorded handler and deleting `latest-receipt.json` in the same operation — is still reproducible on the candidate, via `rename → uninstall` instead of `rename → install → uninstall`. The candidate shut the `install()` door; the `uninstall()` door is untouched. | **P1, blocking** | R6-P1-A, incompletely fixed |

Everything else checked out. (a) fixed on all three variants; (b) no collateral damage; (c) 73/73 on both
interpreters; (d) no regressions in rounds 1–6; (e) no false-positive rejections on near-miss hand-written hooks,
and the refusal itself is a clean pre-transaction refusal with a well-formed CLI error.

This is the **seventh consecutive round** in which the round's own fix introduces or leaves a reproducible P1.

---

## 1. What the change does

`install_bridge.py:915-947`. Inside the `previous_row is None` branch of `install()`'s per-config loop, before
recording the current disk content as the pristine baseline, it re-parses `raw`, pulls the `UserPromptSubmit`
handler list, and refuses if any handler is `owned_handler()`:

```python
if any(owned_handler(handler) for handler in existing_handlers):
    raise InstallError(
        "refusing to record an already-bridged config as a pristine baseline "
        f"(did this path move since the last install? run uninstall first): {path}"
    )
```

This is the round-6 report's proposed minimal fix, transcribed verbatim including its remediation claim —
*"the operator either restores the old path (the carried-forward row then does its job) or runs `uninstall`
first."* **The second half of that claim is false**, and the candidate promoted it from a report sentence into
the user-facing error string and the code comment (`install_bridge.py:934-936`).

Placement is correct: the refusal is inside the pre-transaction loop, before `write_runtime()`, before
`ensure_private_dir(backup_dir)`, and before the pending journal is written. Verified: nothing becomes durable.

---

## 2. (a) R6-P1-A — independently verified as fixed *for the install-adoption path*

Harness: my own fake SSD (`harness.py`), accounts `pool-alpha`/`pool-beta`/`pool-delta`, pristine hook command
`/bin/echo operator-owned` — deliberately none of the candidate test's fixture names, so nothing is reused.
All real, unmocked `plan/install/verify/uninstall/recover`.

### Non-vacuity control (`s_baseline.py`)

Same scenario, both revisions:

```
--- BASELINE f8abefc9f0 ---
install #2 -> ok: SUCCEEDED
  moved row before_sha == after_sha ? True        <-- self-referential baseline
uninstall -> ok: ok=True
  restored lists moved path? True
  owned handlers STILL LIVE in moved config: 1
  latest-receipt.json still present? False

--- CANDIDATE 8fe75b209e ---
install #2 -> err: refusing to record an already-bridged config as a pristine baseline …
```

R6-P1-A reproduces exactly on the baseline and is refused on the candidate. The fix is real, not a no-op.

### Variant results (`s_a_rename.py`) — all PASS

| Variant | Result |
|---|---|
| **① same-release rename** (`mv pool-alpha pool-alpha-moved`) | refuses with `already-bridged`; **no** pending journal; `latest-receipt.json` byte-identical; moved config content untouched. Rename back → `install` + `uninstall` restore all three configs **byte-exactly** to true pristine. |
| **② cross-release upgrade + rename** (hook source changed → new `release_id`) | also refuses; receipt still records the v1 release; moved config untouched. Rename back → upgrade `install` + `uninstall` restore **true** pristine, not the v1-installed bytes. |
| **③ parent relocation** (`codex-accounts/` moved + symlinked back) | refuses on the first pooled account; all accounts affected at once, as the round-6 report predicted. Undo → `uninstall` restores every account pristine. |

`install()`'s adoption door is genuinely shut, in all three shapes.

---

## 3. R7-P1-A — the refusal's own remediation is a permanent lockout *(new, blocking)*

### Reproduction (`s_e_lockout.py` E1) — no crash, race, privilege, or mock

```
install                                   # normal, both configs bridged
mv codex-accounts/pool-alpha codex-accounts/pool-alpha-moved
install    -> err: "…did this path move since the last install? run uninstall first"
uninstall  -> ok: true                    # <-- following the tool's own advice
```

State after that `uninstall`:

```
uninstall  -> ok: ok=True
   restored   = ['local-homes', 'pool-beta']
   unreachable= ['pool-alpha']            # names the OLD path, which no longer exists
latest-receipt.json exists?                          False
owned handlers still LIVE in the moved config:       1
release runtime retained (hook script executable)?   True
```

Every subsequent tool action:

```
plan       -> ok   ok:True
verify     -> err  not installed: no receipt found
recover    -> ok   ok:True {'state': 'none'}      # no-op
uninstall  -> err  not installed: no receipt found
install    -> err  refusing to record an already-bridged config as a pristine baseline …
```

**Renaming the directory back does not rescue it** — the receipt is gone, so the restored path is *still* an
unrecognised path holding already-bridged content:

```
--- after renaming back ---
install    -> err  refusing to record an already-bridged config as a pristine baseline …
                   …/codex-accounts/pool-alpha/home/hooks.json
```

Exhaustive escape hunt (`s_f.py` F3) — `recover`+`install`, `install` twice, rename back, rename forward again:
**every one refuses**. Only out-of-band hand-editing of `hooks.json` recovers the machine.

This is verbatim the failure signature this file's own comments classify as P1 three separate times —
`install_bridge.py:626-627` (*"every action refuses forever except plan, the exact lockout signature round 1 and
round 2's own fixes each separately introduced too"*) and `install_bridge.py:855-861` (the stated reason round 3's
first carry-forward fix was rejected) — now combined with P1-1's consequence: the bridge keeps executing on every
Codex prompt for that account, with no record anywhere that it exists.

### It is new this round (`s_e_baseline_cmp.py`)

```
### base                                        ### cand
state X (receipt lost, configs bridged)         state X
   install   -> ok: SUCCEEDED                      install   -> err (locked)
   uninstall -> ok                                 verify    -> err  not installed
   tool still USABLE afterwards? ok                uninstall -> err  not installed

state Y (relocate, uninstall, retry)            state Y
   install   -> ok: SUCCEEDED (still usable)       install   -> err (locked)
   uninstall -> ok
```

The baseline is *silently wrong* in both states but never bricks. The candidate bricks in both.

### Three independent entry points, all ordinary operations

| # | Entry | Evidence |
|---|---|---|
| 1 | **Relocate an account directory, then uninstall.** The premise of the whole fix is a permanent relocation; "uninstall, then reinstall" is the natural next step, and the error message recommends it. | E1 |
| 2 | **Provision a pooled account by cloning an existing one** (`cp -r pool-alpha pool-gamma`). The error blames a *move* — nothing moved — then `uninstall` → same lockout. | E2 |
| 3 | **Re-provision a fresh account at the old name after relocating** — the **drift** check fires first (`hook config no longer matches the last known installed state (run verify or uninstall first)`), masking the relocation entirely. Follow *that* advice → `uninstall` → `ok:true` → same lockout. | F1 |

Entry 3 is the drift-interaction the review brief asked about, and it is worse than merely confusing: the drift
message wins the race, so the operator is never told a path moved at all, and its remediation is the same trap.

The minimal trigger is simply **`latest-receipt.json` absent while any discovered config is bridged** (E3) — a
wiped `.shared-runtime`, a restore that omits it, or the tool's own `uninstall` above. Before this round that
state was recoverable-but-wrong; now it is unrecoverable.

### Why the new regression test does not catch it

`test_install_refuses_to_adopt_an_already_bridged_config_as_pristine_after_a_rename`
(`tests/test_install_bridge.py:876-918`) exercises only the **first** of the two remediations the error message
offers — rename back, then `install`+`uninstall`. It never runs the **second**, `run uninstall first`, which is
the one printed to the operator. The assertion it does make is sound and non-vacuous (confirmed: `InstallError
not raised` on the baseline); it just tests the safe branch.

### Direction of a fix (not applied — read-only review)

The pristine bytes are not actually lost in any of these states: `update_hook_config(raw, cmd, remove=True)`
reconstructs them from the bridged content, which is exactly what `uninstall()` does. Recovering the baseline
instead of refusing would handle rename, clone, and receipt-loss uniformly with no lockout (`before_mode` would
be the installed `0o600` rather than the original mode — a far smaller defect than a brick). Alternatively, keep
the refusal but give `uninstall` a way to strip owned handlers from *discovered-but-unreceipted* configs, so the
advertised remediation actually works. Either way the invariant to assert is the one rounds 1–3 already
established: **no input may leave every action refusing except `plan`.**

---

## 4. R7-P1-B — R6-P1-A's stated harm is still reproducible *(blocking)*

R6-P1-A was reported as: *"uninstall() … reports ok:true, lists the renamed path in restored, but the bridge
handler is not removed and is still executing — and latest-receipt.json is deleted in the same operation, so the
only record that could expose it is gone."*

On the candidate, from E1 above:

```
uninstall  -> ok: ok=True
   unreachable = ['pool-alpha']
latest-receipt.json exists?                     False
owned handlers still LIVE in the moved config:  1
release runtime retained?                       True
```

The `restored` list no longer names the moved path — the one improvement — and `unreachable` carries a hint. But
that hint names the **old, now-nonexistent** path and says nothing about the relocated file that is actually
still bridged; the operator is told `ok: true` and the receipt is destroyed in the same call. The bridge keeps
injecting Claude memory into that account's Codex prompts after the operator has been told it is gone.

Fixing `install()` alone could never close this: reaching `uninstall` never requires passing through the new
check. R6-P1-A needs a fix on the `uninstall`/`verify` side as well — at minimum, `uninstall` must not report
`ok:true` and delete the receipt while a *discovered* config still carries an owned handler it did not account for.

---

## 5. (b) No collateral damage to the temporary rename-and-back path

`s_bde.py` (b) — all PASS, two independent flavours (neither reusing the candidate test's naming):

| Flavour | Result |
|---|---|
| **b1** `hooks.json` itself parked and restored (the `test_install_carries_forward_…` shape) | install while parked succeeds; path carried forward into the new receipt; `verify` ok; **install after restore is not falsely refused**; `uninstall` restores pristine byte-exactly |
| **b2** whole account **directory** parked *outside* `codex-accounts/` and moved back | install while parked succeeds; install after restore not falsely refused; `uninstall` restores pristine exactly |

The round-3 carry-forward machinery is intact. The new check correctly does not fire on a carried-forward row,
because such a row *is* in `previous_rows_by_path` once the path reappears.

---

## 6. (c) Test suites — 73/73 on both interpreters, genuinely green

| Interpreter | Result |
|---|---|
| `/usr/bin/python3` **3.9.6** (the documented one) | `Ran 73 tests … OK` |
| PATH `python3` = `/opt/homebrew/bin/python3` **3.14.6** | `Ran 73 tests … OK` |

`install_bridge` test methods: **34** (33 → 34, as claimed). No skips, no expected failures, no xfails.

---

## 7. (d) Rounds 1–6 regression spot-checks — no regressions

| Round | Item | Independent check | Result |
|---|---|---|---|
| 1 | `owned_handler` exact `--bridge-id` argv pair | exact pair / substring / shell comment / flagless mention | **PASS** (4/4) |
| 1 | 5+2 guarded `stat()` sites | `_mode_bits` on a missing path | **PASS** — `InstallError`, not `OSError` |
| 1 | P1-2 uninstall transaction journal | suite's SIGKILL-mid-uninstall test on both interpreters | **PASS** |
| 2 | R2-P1-A `prev_*` / `before_*` separation | interrupted **upgrade** (injected write failure on one account) | **PASS** — rolls back to the v1 *installed* state, not pristine; journal cleared; `verify` ok; later `uninstall` reaches **true** pristine |
| 2 | R2-P1-B lock-dir creation mode | `_acquire_exclusive_lock()` with `.shared-runtime` absent | **PASS** — created `0o700`; install still works |
| 3 | R3-P1-A permanently retired account | `rmtree` an account → install / verify / uninstall | **PASS** — `unreachable`, no lockout, other accounts restored pristine |
| 3/4 | R3-P2-A / R4-P2-A pruned backup dir tolerance | prune a carried-forward row's backup dir, then install | **PASS** |
| 4 | R4-P1-A fail-closed on an unreadable config | `chmod 0o000` an account home | **PASS** — `verify`/`uninstall` both fail closed with `cannot determine whether … exists`; receipt **not** deleted |
| 5 | R5-P1-A eager backup load in `uninstall` | `rm -rf` the whole backups dir, then uninstall | **PASS** — refuses, **no** pending-journal wedge, configs untouched |
| 5 | R5-P2-A eager carried-forward determination | park a config, then `chmod 0o000` its home | **PASS on 3.14**, blocked by R6-P2-B on 3.9 (below) |

### Deliberately deferred items — confirmed still present, unchanged

**R6-P2-B** (= R5-P3-A / R3-P3-B) independently reproduced and confirmed interpreter-dependent:

```
3.9.6  -> ('crash', "PermissionError: [Errno 13] Permission denied: …")   # raw traceback, not {"ok": false}
3.14.6 -> ('err',   'cannot determine whether … exists')                  # clean InstallError
```

On the documented `/usr/bin/python3` 3.9, `discover_hook_configs()`'s `candidate.is_file()` still bare-raises, so
round 5's R5-P2-A fix is unreachable for this exact input on the exact interpreter the release targets. Matches
the deferral note exactly; not a round-7 regression, and not what makes this NO-GO — but worth noting that a
deferred P2 is *currently observable as a raw Python traceback on the shipping interpreter*.

R6-P2-A and R6-P2-C were not re-derived; nothing in this round's diff touches them.

---

## 8. (e) New-defect probes beyond R7-P1-A/B

### No false-positive rejections on near-miss hooks — all PASS

Fresh (never-installed) machine, an operator-written hook alongside a normal one:

| Vector | Fresh install | Hook preserved | `uninstall` exact |
|---|---|---|---|
| `echo pre-<BRIDGE_ID>-post` (substring) | not refused | yes | yes |
| `/usr/bin/true # --bridge-id <BRIDGE_ID>` (shell comment) | not refused | yes | yes |
| `run --other-flag <BRIDGE_ID> --bridge-id something-else` | not refused | yes | yes |

The new check reuses `owned_handler()`, so it inherits round 1's structural-match discipline exactly. **No
false-positive class was introduced.**

The one vector that *does* trip it is a foreign hook carrying a literal, adjacent `--bridge-id <BRIDGE_ID>` argv
pair on a never-installed machine — which refuses the **first-ever** install with a message about a move that
never happened, and no way forward. Pre-fix behaviour was to silently **delete** that foreign hook, so refusing
is defensible in isolation; its real weight is that it is another door into the R7-P1-A lockout. Rated on its own
this vector is P3 (a foreign tool reusing this exact BRIDGE_ID is implausible); it is listed here only for
completeness.

### Refusal hygiene — clean

- **CLI surface** (`s_f.py` F2): through real `main()` dispatch — exit code `1`, well-formed
  `{"ok": false, "error": …}`. No traceback.
- **Pre-transaction**: no pending journal, receipt byte-identical, target file content untouched. Confirmed on
  every refusing variant.
- **Double-parse safety**: the new `strict_json(raw)` runs *after* `update_hook_config(raw, …)` has already
  enforced `{"hooks": {"UserPromptSubmit": [...]}}`, so the `.get("hooks", {}).get(…)` chain cannot hit a
  non-dict `hooks`. Probed directly with `{"hooks": ["not-a-dict"]}` → clean
  `InstallError: unexpected hooks.json structure`. The `isinstance(existing_payload, dict)` guard is
  belt-and-braces on an unreachable branch; harmless.

### P3 — `plan()` and `install()` disagree

After a rename, `plan` reports `ok:true` with `will_change: false` for the very config `install` refuses
(`update_hook_config` is idempotent and the mode is already `0o600`). `plan` is advisory and the divergence is
harmless on its own, but it means the one action that still works in the locked state actively reports the
machine as healthy. Worth aligning once R7-P1-A is addressed.

---

## 9. Reproduction assets

All under the session scratchpad
(`…/claude-code-runtime/claude-501/-Volumes-Extreme-SSD-Orca-workspaces-orca---orca/0ff833af-…/scratchpad/r7/`),
run in isolated temp roots against monkeypatched module constants — the real `/Volumes/Extreme SSD` Codex
`hooks.json` files were **never** touched, and no install/activation was performed. The candidate tree is
byte-unchanged (`git status --porcelain -- claude-codex-memory-bridge/` is empty).

| File | Purpose |
|---|---|
| `harness.py` | independent fake-SSD sandbox + tool-action matrix helper |
| `s_a_rename.py` | (a) variants ①②③ |
| `s_baseline.py` | non-vacuity control, baseline vs candidate |
| `s_e_lockout.py` | R7-P1-A entry points E1/E2/E3 |
| `s_e_baseline_cmp.py` | proves the lockout is new this round |
| `s_bde.py` | (b) carry-forward, (d) rounds 1–6, (e) false-positive probes |
| `s_f.py` | drift interaction, CLI surface, exhaustive escape hunt |

---

## 10. Bottom line

The round-7 change is **correct in what it refuses and clean in how it refuses**: three rename variants closed,
no false positives, no regressions across six rounds of prior fixes, 73/73 on both interpreters, and a
well-formed pre-transaction refusal. Had this been the whole picture it would be a straightforward GO.

It is not, for one reason: the fix was transcribed from a report sentence that asserted two valid remediations,
and only one of them is real. The other — the one printed in the error message — leads to a machine where the
bridge is still running, the receipt is destroyed, and no tool action will ever work again. R6-P1-A's own stated
harm survives alongside it, because `install()` was never the only door to it.

**NO-GO.** Both P1s must be closed and re-reviewed against a fresh candidate. The regression test to demand is
the one the invariant has needed since round 1: *for every input state, at least one tool action other than
`plan` must make progress, and `uninstall` must never report `ok:true` while a discovered config still carries an
owned handler.*
