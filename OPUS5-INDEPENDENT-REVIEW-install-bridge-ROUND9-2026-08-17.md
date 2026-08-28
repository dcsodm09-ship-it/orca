# Independent read-only review — `install_bridge.py`, round 9

**Reviewer:** Claude `opus5` / effort `max`, independent, no knowledge of the parallel round-9 review.
**Candidate:** `cb1c532459` — *fix(install_bridge): close the four ways round 8's untracked-handler scan could still be bypassed*
**Baseline:** `37d2433a77` (round-8 review candidate)
**Scope:** `claude-codex-memory-bridge/install_bridge.py`, `claude-codex-memory-bridge/tests/test_install_bridge.py`
(`git diff 37d2433a77 cb1c532459 -- claude-codex-memory-bridge/` = +352/−48 over 2 files)
**Date:** 2026-08-17
**Method:** every reproduction below ran in a private throwaway fake-SSD tree under the session scratchpad, in
separate processes, through the installer's real `main()` CLI, on both `/usr/bin/python3` 3.9.6 and PATH
`python3` 3.14.6. My harness reuses none of the candidate's test fixtures — own account names
(`pooled-alpha/beta/gamma`), own pristine `hooks.json` bytes, own kill points, own relocation targets, own
malformed payloads. The real `/Volumes/Extreme SSD` Codex configuration was **never** installed into and never
written; the only real-tree operations were read-only `os.walk`/`stat`/`grep` measurements for the performance
question in (h).

---

## Verdict: **NO-GO**

Everything the round-9 commit set out to fix, it fixed. I independently reproduced all six round-8 findings'
remediation, with my own fixtures, on both interpreters: **R8-P1-A, R8-P1-B, R8-P1-C, R8-P1-D and R8-P2-A are
genuinely closed**, and R8-P2-B's directory-listing and `stat()` halves are closed *and* now behave identically
on 3.9 and 3.14. 79/79 on both interpreters. No regression in any round 1–7 fix I could exercise (11/11
spot-checks). The `os.walk()` cost is a non-issue on the real machine (0.24 s over 5 694 directories).

But the same harm this whole round 6→9 series exists to eliminate — `ok:true`, the receipt or journal deleted,
a **live owned handler abandoned at a path nothing tracks**, and then no tool action that can ever find it — is
still reproducible on the candidate through **two independent doors**, on both interpreters, with no crash,
no race and no privilege beyond an ordinary `mv` and (for the second) an ordinary `chmod`:

| ID | Finding | Severity | Novelty |
|----|---------|----------|---------|
| **R9-P1-A** | `recover_pending_install()`'s **`kind == "install"` rollback branch** has no untracked-owned-handler scan. A real `SIGKILL` mid-install + one `mv` of an already-written account ⇒ `recover` reports `ok:true / rolled_back`, deletes the journal, and abandons a live owned handler with **no receipt existing anywhere**. Sibling rename ⇒ every action refuses forever *and renaming back does not help* (R7-P1-B verbatim). Subfolder move ⇒ `install` afterwards **succeeds**, and the orphan executes on every Codex prompt forever. | **P1, blocking** | pre-existing; squarely inside this round's stated scope (round-8 GO condition #1) |
| **R9-P1-B** | `_find_untracked_owned_configs()`'s `except InstallError: continue` around `validate_owned_file()` is **fail-open on "cannot determine"**. The identical relocation that the scan correctly refuses becomes `ok:true` + receipt deleted + permanent orphan after a single `chmod g+w` (or `chmod 000`) on the relocated `hooks.json`. Identical on 3.9 and 3.14. This is R4-P1-A's exact defect at a new call site — and the surviving half of R8-P2-B. | **P1, blocking** | **new this round** (introduced by the new function) |
| **R9-P2-A** | The walk root is `LOCAL_HOMES_ROOT`. An account relocated *outside* `local-homes` but still on the SSD is invisible ⇒ same silent-abandonment harm. The code comment is honest about this bound ("regardless of where **under local-homes** it landed"), so this is a scope limit rather than a false claim — but it is the concrete shape of round 8's warning that "a directory search will always be one `mv` behind". | P2 | new this round (inherent to the chosen approach) |
| **R9-P2-B** | The commit's claim to have "彻底堵上 R8-P2-B" / closed R5-P3-A / R3-P3-B / R6-P2-B is **not accurate**. Two of the three halves are genuinely closed and now interpreter-consistent; the file-read half is fail-open on **both** interpreters (that is R9-P1-B). Round 8's exposure was at least bounded to 3.13+; this one is not. | P2 | claim overstated |
| **R9-P3-A** | A relocation racing an *in-flight* uninstall leaves a pending journal nothing can clear, **and** the revert loop resurrects a phantom account directory (`atomic_write()`'s `mkdir(parents=True)`) which then blocks the documented "rename it back" remedy with `ENOTEMPTY`. Recoverable by hand only. | P3 | pre-existing, newly interacting with the scan |
| **R9-P3-B** | Two more silent skips in the same `except InstallError: continue` family: a `hooks.json` that is a symlink to a target off the SSD, and an account relocated *into* the skipped runtime subtree. | P3 | new this round |
| **R9-P3-C** | The new `test_uninstall_ignores_untracked_configs_with_malformed_or_unexpected_content` is **vacuous**: it passes unchanged on `37d2433a77`. It places its malformed files outside the one-level shape round-8's scan enumerated, so round-8 never looked at them. The dispatch's "5 个新测试均已对照基线做过非空转验证" is wrong for this one. The other 4 are genuinely non-vacuous. | P3 | new this round |

Confirmed clean: (a), (b), (c), (d) all pass; (e) passes for the two halves it covers; (f) 79/79 × 2
interpreters; (g) 11/11; no false positives; performance fine.

This is the **ninth consecutive round** in which the round's own change either introduces or leaves a
reproducible P1. Round 8's report closed with a warning that the search-based approach "will always be one
`mv` behind" and proposed a precondition-based check instead. R9-P1-A, R9-P1-B, R9-P2-A and R9-P3-B are four
different instances of exactly that prediction.

---

## 1. What the change does

Four edits, all in the untracked-owned-handler safety scan's neighbourhood:

1. **`_is_regular_file(path)`** (`:220-240`) — `Path.stat()` + explicit `FileNotFoundError` / other-`OSError`
   discrimination, replacing `Path.is_file()` at `_enumerate_hook_configs():270` and inside the new walk.
2. **`_find_untracked_owned_configs(receipt_paths)`** (`:357-425`) — bounded `os.walk()` over the whole
   `local-homes` tree, `followlinks=False`, pruning the bridge's own runtime subtree, with an explicit
   `onerror` callback that lets `FileNotFoundError` pass and escalates every other `OSError` to `InstallError`.
3. **`_contains_owned_handler(raw)`** (`:326-354`) — all parse/structure failures funnelled to `False`.
4. The scan now runs at **two** call sites: `uninstall():1376` (as before) and, new, at the very top of
   `recover_pending_install()`'s `kind == "uninstall"` branch (`:924-931`).

---

## 2. (a) — R8-P1-A is genuinely fixed

`scratchpad/scenario_a.py`. Four managed configs (`.codex` + three pooled accounts), own kill point: SIGKILL
after the 3rd `atomic_write` of `uninstall()` — i.e. the pending journal plus two reverts, leaving
`pooled-beta` **and** `pooled-gamma` still bridged. Then `mv codex-accounts/pooled-gamma
codex-accounts/pooled-gamma--parked-by-reviewer` (an account the interrupted attempt had not reached).

```
[2] child rc=-9   (real SIGKILL, not an in-process exception)
    pending journal exists: True     latest receipt exists: True
    owned map: .codex=false  pooled-alpha=false  pooled-beta=true  pooled-gamma=true

[4] every ordinary recovery entry point:
    recover   rc=1  refusing to finish uninstall: ... does not track ... pooled-gamma--parked-by-reviewer/home/hooks.json
    uninstall rc=1  (identical)
    install   rc=1  (identical)
    -> receipt intact, pending journal intact, relocated handler intact, nothing written

[5] mv back; recover -> rc=0 {"ok":true,"state":"uninstalled"}
[6] all four configs byte-pristine, no owned handler, no receipt, no pending journal
```

Identical on 3.9.6 and 3.14.6. **Non-vacuity:** the same script against `37d2433a77` gets
`recover -> rc=0 {"ok":true,"state":"uninstalled"}` at step 4 — receipt deleted, `pooled-gamma--parked-by-reviewer`
still bridged. The fix is real.

## 3. (b) — R8-P1-B is genuinely fixed for every shape under `local-homes`

`scratchpad/scenario_bcde.py`, four relocation shapes I chose myself, each `install` → relocate → `uninstall`:

| shape | result |
|---|---|
| `codex-accounts/pooled-beta` → `codex-accounts/_attic/2026-q3/pooled-beta` (two levels deeper) | **rc=1, refuses**, names the new path |
| `codex-accounts/pooled-gamma` → `local-homes/detached-homes/gamma` (out of `codex-accounts`) | **rc=1, refuses** |
| `codex-accounts/pooled-alpha/home` → `.../home.disabled` (inner `home/` renamed) | **rc=1, refuses** |
| `local-homes/.codex` → `local-homes/.codex-previous` (the main home itself — my own extra) | **rc=1, refuses** |

In every case the receipt survives, no pending journal is written, and the relocated handler is untouched.
Same on both interpreters. The one shape that is **not** caught is `local-homes/codex-accounts/pooled-beta` →
`ssd/Orca/parked-accounts/pooled-beta` (outside the walk root) — see **R9-P2-A** below.

## 4. (c) — R8-P1-C is genuinely fixed

Three genuine-`ENOENT` variants, each `rm -rf` (not a rename):

- `rm -rf codex-accounts/pooled-beta` ⇒ `uninstall` **rc=0**, restores `.codex`/`alpha`/`gamma` byte-pristine,
  reports `pooled-beta` in `unreachable`.
- `rm -rf .codex` ⇒ `uninstall` **rc=0**, restores all three accounts, `.codex` in `unreachable`.
- `rm -rf .codex && rm -rf codex-accounts` ⇒ `uninstall` **rc=0**, `restored: []`, all four `unreachable`.

R3-P1-A's tolerance is preserved: a genuinely deleted account no longer aborts the transaction, and the walk
degrading a missing directory to "nothing there" is what makes that work.

## 5. (d) — R8-P1-D and R8-P2-A are genuinely fixed

Eleven malformed/odd payloads of my own (`{"hooks": 3.5}`, `{"hooks": {"UserPromptSubmit": {"zero": 1}}}`,
`[1,2,3]`, `this is definitely not json {{{`, `{"hooks": {"UserPromptSubmit": [null, 7, "x", true]}}`,
`{"hooks": {"UserPromptSubmit": [{"hooks": "not-a-list"}]}}`, duplicate top-level keys, invalid UTF-8, empty
file, top-level string, `{"hooks": {"UserPromptSubmit": [{"hooks": [null, {"command": null}]}]}}`), all at
untracked paths:

- **11 malformed files present** ⇒ `uninstall` **rc=0**, every managed config byte-pristine. No crash, no
  empty stdout, no blocked uninstall.
- **the same 11 plus one genuinely owned untracked config** ⇒ `uninstall` **rc=1**, naming exactly the one
  real path. No false negative and no false positive.

Both interpreters. See **R9-P3-C** for a caveat about the candidate's own regression test for this item.

## 6. (e) — R8-P2-B: two halves closed, one half open

Five permission scenarios, each run on **both** interpreters. The point of R8-P2-B was that 3.9 and 3.13+
disagreed; the useful result is that they now **agree everywhere** — but they agree on the wrong answer in two
of the five:

| scenario | 3.9.6 | 3.14.6 | verdict |
|---|---|---|---|
| unrelated dir under `local-homes` `chmod 000` | rc=1 `cannot list …` | rc=1 identical | **fixed, consistent** |
| relocated account, its `home/` dir `chmod 000` | rc=1 `cannot list …` | rc=1 identical | **fixed, consistent** |
| relocated account, its `home/` dir `chmod 600` (r, no x) | rc=1 `cannot determine whether … is a managed config` | rc=1 identical | **fixed, consistent** |
| relocated account, `hooks.json` itself `chmod 000` | **rc=0, receipt deleted, orphan survives** | **rc=0, identical** | **R9-P1-B** |
| relocated account, `hooks.json` mode `0664` (still world-readable and live) | **rc=0, receipt deleted, orphan survives** | **rc=0, identical** | **R9-P1-B** |

The directory-listing half (via `onerror`) and the `stat()` half (via `_is_regular_file()`) are genuinely and
consistently closed. The **file-read** half is not.

## 7. (f) — test suite

```
/usr/bin/python3 3.9.6   ->  Ran 79 tests in 0.828s   OK   (79 "... ok", 0 skips)
PATH python3 3.14.6      ->  Ran 79 tests in 0.692s   OK   (79 "... ok", 0 skips)
```

`install_bridge` methods: 35 → 40, as stated. Non-vacuity of the 5 new tests against `37d2433a77`
(baseline installer + candidate tests): **4 of 5 fail on the baseline** — `…relocated_outside_the_one_level_
discovery_shape` FAIL, `…unrelated_directory_becomes_unlistable` FAIL, `…codex_home_itself_is_unreachable`
ERROR, `…sigkill_mid_uninstall_then_relocating…` FAIL. The fifth passes on the baseline (**R9-P3-C**).

## 8. (g) — rounds 1–7 spot-checks: 11/11 clean

Run through the real `main()` CLI, on both interpreters, all passing:

| item | check | result |
|---|---|---|
| P1-2 | uninstall journaled; plain round-trip restores byte-exact and clears the receipt | ✔ |
| `owned_handler` precision | 4 BRIDGE_ID decoys (substring / shell comment / wrong flag / value-only) placed as untracked configs do **not** trip the new scan | ✔ no false positive |
| stat guards | every failure in every scenario surfaced as clean `{"ok": false, "error": …}` JSON; no bare traceback anywhere | ✔ |
| R2-P1-A | SIGKILL mid-**upgrade** ⇒ `rolled_back`, all 4 configs still bridged at the *previous working* release, `verify` passes | ✔ |
| R2-P1-B | first `install` on a machine with no `.shared-runtime` at all (lock path builds the dir chain) | ✔ |
| R3-P1-A | retired account carried forward; second install ok; uninstall ok with it in `unreachable` | ✔ |
| R3-P2-A / R4-P2-A | carried-forward row's old backup directory pruned ⇒ `verify` ok, `uninstall` ok, all live configs pristine | ✔ |
| R4-P1-A | unreadable **tracked** config ⇒ `uninstall` and `verify` both fail closed, receipt kept | ✔ |
| R5-P1-A | live row's backup dir `rm -rf`'d ⇒ clean refusal, **no** pending journal left, receipt kept | ✔ |
| R5-P2-A | carried-forward row merely indeterminate (`chmod 000`) ⇒ `install` refuses before writing anything | ✔ |
| R6-P1-A | receipt lost + account renamed ⇒ `install` refuses to adopt already-bridged content as pristine | ✔ |
| R7-P1-A/B | uninstall-side untracked scan ⇒ see §3 | ✔ |

---

# The blocking findings

## 9. R9-P1-A — the install-direction rollback commits with no scan at all

**`install_bridge.py:864-907`** (`recover_pending_install()`, `kind == "install"`). Round 9 added the scan to
the `kind == "uninstall"` branch only. The install branch is *also* a durable-journal commit that reports
`ok:true` and deletes the journal, and for a **first-ever install there is no `latest-receipt.json` at all** —
so the end state is strictly worse than R8-P1-A's: not "the receipt was deleted", but "a receipt never existed".

The rollback loop skips any row whose path is now `absent` (`:897-899`, `install_state != "after"`), which is
exactly what a relocated account's row becomes.

### Reproduction (`scratchpad/scenario_h1.py`, both interpreters)

`install()`'s write order on a 4-config machine is: 2 runtime files, 8 backups, `receipt.json`, the pending
journal (#12), then the four live configs (#13–#16), then `latest-receipt.json` (#17). Killing anywhere in
#13–#16 is an ordinary interruption window — that is where the install spends its time on disk.

```
fresh machine, never installed
$ install                       # real SIGKILL after write #14
  -> bridged: .codex, codex-accounts/pooled-alpha ; pending=True ; receipt=False
$ mv codex-accounts/pooled-alpha codex-accounts/pooled-alpha--relocated
$ recover
  {"ok": true, "state": "rolled_back"}          <-- reports success
  pending=False  receipt=False  orphan-still-bridged=True
$ verify     -> {"ok": false, "error": "not installed: no receipt found"}
$ uninstall  -> {"ok": false, "error": "not installed: no receipt found"}
$ install    -> {"ok": false, "error": "refusing to record an already-bridged config as a pristine baseline …"}
$ mv codex-accounts/pooled-alpha--relocated codex-accounts/pooled-alpha    # the documented remedy
$ verify / uninstall / install  -> identical refusals; only `plan` still returns ok
  original path bridged after the remedy: True
```

Every action refuses forever and **renaming back does not help**, because with no receipt at all there is
nothing for `install()`'s R6-P1-A guard to match against. That is R7-P1-B's signature exactly, on the round-9
candidate.

The second variant is quieter and worse:

```
$ mv codex-accounts/pooled-alpha codex-accounts/retired/2026/pooled-alpha   # invisible to discovery
$ recover    -> {"ok": true, "state": "rolled_back"}
$ install    -> {"ok": true, …}                 <-- a completely normal, successful install
  orphan bridged: True
```

`install` succeeds, writes a fresh receipt covering the three discoverable configs, and **recreates the release
directory the orphan's command points at** — so the abandoned handler is not merely stale, it executes on every
Codex prompt in that account, forever, with no record anywhere. (`uninstall` afterwards does refuse — the round-9
scan sees it — but by then the operator has a machine with two bridged trees and no supported way to
disentangle them: renaming back leaves *both* copies bridged.)

**Why this is in scope, not a pre-existing out-of-scope bug:** round 8's GO condition #1 was worded as "so **no
journal commit** can delete the receipt while an untracked owned handler is reachable", and the new comment at
`:909-923` states the design intent as "this is the OTHER place a receipt-deleting commit happens". There is a
third: the install-direction rollback, 20 lines above, in the same function.

## 10. R9-P1-B — the scan's own `except InstallError: continue` is fail-open

**`install_bridge.py:419-422`**

```python
try:
    candidate_raw = validate_owned_file(resolved)
except InstallError:
    continue
```

`validate_owned_file()` is a **write-safety** gate: it refuses a file that is unreadable (`EACCES` on `os.open`),
that is not owned by us (`st_uid != os.getuid()`), that is group- or other-writable (`st_mode & 0o022`), or that
is larger than 4 MiB. Those are the right questions for "may I rewrite this file?". They are the wrong questions
for "does this file contain a live handler of mine?", which is all the scan needs to know — and every one of
them silently answers "no handler here".

The function's own header comment states the opposite discipline for directories: *"only ENOENT … means 'not
present'; anything else means 'cannot determine' and must fail closed"*. Twenty lines below, the file-read step
does not follow it. This is `_path_is_absent()`'s R4-P1-A comment almost verbatim — *"a managed config that is
merely unreadable right now — still on disk, still holding a live bridge handler — was misclassified as
absent: uninstall() then reported ok:true, deleted latest-receipt.json, and permanently lost the true baseline
while the handler kept executing"* — reappearing at a new call site.

### Reproduction (`scratchpad/scenario_e4.py`, identical on 3.9.6 and 3.14.6)

The control is the important part: the *same relocation* is correctly refused, and one `chmod` flips it.

```
$ install                                              # 4 configs bridged
$ mv codex-accounts/pooled-gamma retired-accounts/gamma     # the §3 "out of codex-accounts" shape
$ uninstall   -> rc=1  REFUSES (correct)
$ chmod 0664 retired-accounts/gamma/home/hooks.json    # still world-readable; handler still live
$ uninstall   -> rc=0  {"ok": true, …}                 <-- receipt deleted
    orphan still carries the bridge handler: True
$ verify      -> {"ok": false, "error": "not installed: no receipt found"}
$ uninstall   -> {"ok": false, "error": "not installed: no receipt found"}
$ install     -> rc=0   (succeeds; rebuilds the release dir the orphan points at)
    orphan STILL bridged at the end: True
```

`chmod 0000` on the same file behaves identically (`ok:true`, receipt deleted, orphan intact) — and there the
operator can restore the handler to full liveness at any later moment with a single `chmod`, with no record
that it exists.

**Plausibility of the trigger.** `0664` is what you get from a `cp`/`rsync`/archive-extract under `umask 002`,
from a restore that does not preserve modes, or from any tool that writes with `0666 & ~umask`; a `sudo`-driven
restore produces the `st_uid != getuid()` variant, which fails open the same way while remaining perfectly
readable by Codex. None of that requires a crash, a race, or a privileged attacker. The exposure is also
strictly wider than R8-P2-B's, which round 8 rated P2 partly *because* the documented `/usr/bin/python3` 3.9
invocation still failed closed there. Here both interpreters fail open.

**Note on the naive fix:** simply propagating the `InstallError` would be worse. The real `local-homes` tree
already contains six unrelated `hooks.json` files (Claude Code plugin hooks under
`.claude/plugins/marketplaces/…/hooks/hooks.json`); a blanket fail-closed would let any one of them, if it ever
acquired an odd mode or owner, block every `uninstall` forever. The distinction that matters is *why* the read
failed: "not a file we can identify" may be skipped; "we could not read it" must not. Reading for **detection**
does not need `validate_owned_file()`'s write-safety policy at all — it needs `O_RDONLY|O_NOFOLLOW` plus a size
cap, with any read failure escalated.

---

# Non-blocking findings

## 11. R9-P2-A — the walk root bounds the search to `local-homes`

`mv local-homes/codex-accounts/pooled-beta ssd/Orca/parked-accounts/pooled-beta` (outside the walk root, still
on the SSD) ⇒ `uninstall` **rc=0**, receipt deleted, `parked-accounts/pooled-beta/home/hooks.json` still owned.
Both interpreters. The comment at `:361-370` is accurate about its own bound ("regardless of where **under
local-homes** it landed"), so this is a documented scope limit, not a false claim — and widening the root to
the whole SSD would be a far worse trade. But it is the literal shape of round 8's warning that a directory
search is always one `mv` behind, and the harm when it happens is the full P1 harm.

## 12. R9-P2-B — the "彻底堵上 R8-P2-B" claim

Two of three halves are closed and now interpreter-consistent (§6). The third is R9-P1-B, open on both
interpreters. R5-P3-A / R3-P3-B / R6-P2-B should still be recorded as **partially** fixed. Separately, eight
bare `Path.exists()` / `Path.is_symlink()` call sites on runtime paths (`:196, :581, :836, :855, :986, :1258,
:1261, :1468`) still swallow `EACCES` — pre-existing, outside this diff, and not claimed fixed, but they are
the same family and are worth listing when this file's fail-closed contract is finally written down.

## 13. R9-P3-A — a relocation racing an in-flight uninstall

Relocating an unreverted account *while* `uninstall()` is between its scan and its commit:

```
uninstall -> {"ok": false, "error": "uninstall failed and the durable recovery journal remains pending"}
  pending journal left behind: True     receipt: already deleted
  recover / uninstall / install -> all refuse (correctly) ; verify -> "pending … must be recovered first"
  plan -> ok
```

Recoverable, but two wrinkles make it nastier than it needs to be. First, the revert loop had already written
pristine content to the *old* path, and `atomic_write()` does `path.parent.mkdir(parents=True, exist_ok=True)`
(`:466`) — so it **resurrects a phantom account directory** for an account that has moved. Second, that phantom
directory then blocks the refusal message's own remedy: `mv pooled-gamma--raced pooled-gamma` fails with
`ENOTEMPTY`. The operator must delete the resurrected directory first, which nothing tells them. Pre-existing
resurrection behaviour, newly load-bearing now that the scan makes the rename-back the documented remedy.

## 14. R9-P3-B — two more silent skips in the same `continue` family

- A `hooks.json` that is a **symlink whose target is off the SSD** and carries an owned handler:
  `resolve_ssd_path()` raises "path is outside Extreme SSD" → `continue` → `uninstall` **rc=0**, receipt
  deleted, handler live. (Contrived: such a config could never have been installed by this tool, since
  `discover_hook_configs()` would have refused it — it can only arise by hand or by a later substitution.)
- An account relocated *into* `.shared-runtime/claude-codex-memory-bridge/` is pruned from the walk by
  design (`:406-408`) and therefore invisible.

## 15. R9-P3-C — one of the five new tests is vacuous

`test_uninstall_ignores_untracked_configs_with_malformed_or_unexpected_content` (tests `:1062-1098`) places its
payloads at `local-homes/orphaned-config/hooks.json` — deliberately outside `codex-accounts/<name>/home/`.
Round-8's scan enumerated only `_enumerate_hook_configs()`, which never included that path, so round 8 never
looked at those files. Running the candidate's test file against the `37d2433a77` installer:

```
test_uninstall_ignores_untracked_configs_with_malformed_or_unexpected_content ... ok      <-- passes on the buggy baseline
```

Moving the identical content one directory over reproduces both round-8 bugs on the baseline and confirms the
candidate fixes them:

```
baseline 37d2433a77, at codex-accounts/pooled-delta/home/hooks.json
  {"hooks": []}     -> rc=1, stdout EMPTY, bare AttributeError traceback     (R8-P1-D)
  not json at all   -> rc=1, {"ok": false, "error": "invalid hook JSON"}     (R8-P2-A, no path named)
candidate cb1c532459, same location
  both              -> rc=0, uninstall completes normally
```

So the **fix** is real and verified; the **test** does not pin it against the shape that actually failed, and
the dispatch's non-vacuity claim is inaccurate for this one test.

## 16. (h) — performance, `onerror`, and the systemic question

**Performance: fine.** Measured read-only on the real `/Volumes/Extreme SSD/Orca/local-homes` (APFS): **5 694
directories, 49 035 files**; a full `os.walk` + per-directory `stat(<dir>/hooks.json)` takes **0.24 s** warm.
A synthetic tree with 3 736 directories gives `_find_untracked_owned_configs()` = **0.17 s**. Two notes for the
record: (i) the cost is O(entire `local-homes` tree), not O(accounts), and the growing part is
`.claude/projects` transcripts, not Codex accounts — it will drift upward but stays linear and small; (ii) the
scan runs on every `uninstall` and on every `recover` **that has a pending uninstall journal** — a plain
`recover`/`install` on a healthy machine walks nothing, because `recover_pending_install()` returns early.
Nine `hooks.json` files exist under the real tree: three managed-shaped (`.codex` plus two pooled accounts)
and six unrelated Claude Code plugin hook files (all mode 0644, uid 501). The six unrelated ones are opened and
parsed on every uninstall, and all classify correctly as not-owned. `grep -rl
orca-claude-native-memory-v1 local-homes/` finds only conversation transcripts — **no real config anywhere
under `local-homes` would trip the scan today**, and `os.walk` reports **0 `onerror` events** on the real tree,
so the new fail-closed directory rule blocks nothing on the target machine as it stands. It does create a new
coupling worth documenting: *any* unlistable directory anywhere under `local-homes`, however unrelated to
Codex, now blocks every `uninstall` and every pending-uninstall `recover`.

**`onerror` propagation: correct, with one documented bypass.** `InstallError` is not an `OSError`, so raising
it from the callback propagates out of the generator cleanly on both interpreters — verified end to end (clean
`{"ok": false, "error": "cannot list …"}` JSON, no traceback, on 3.9's recursive `_walk` and on 3.14's
stack-based `walk`). `exc.filename` is populated for `scandir` failures, so the message names the directory.
The one bypass: both implementations catch `entry.is_dir()`'s `OSError` internally and treat the entry as a
non-directory **without calling `onerror`** (3.14 `os.walk` lines 87-90). On APFS `d_type` is populated, so
`is_dir()` needs no `stat()` and I could not construct this; it becomes reachable only on filesystems that
return `DT_UNKNOWN` (some FUSE/network mounts). Worth a line in the preconditions, not a finding.

**The systemic question.** Yes — the new `recover_pending_install()` call inherits exactly the weakness round 8
flagged. Round 8's alternative was: *"refuse the receipt-deleting commit whenever any receipt row is `absent`
and its recorded content was `after`-state … an `absent` row that was still bridged at the last known state is
the actual signal; a directory search will always be one `mv` behind."* The candidate chose the search.
**Every one of R9-P1-A, R9-P1-B, R9-P2-A and R9-P3-B is a place the search cannot reach — and in all four, the
receipt/journal row for the relocated account is `absent` with a last known state of `after`.** The
precondition check would catch all four with no walking at all. It cannot simply replace the search, because a
genuinely retired account (R3-P1-A) produces the same signal — so the shape that actually closes this is a
hybrid: walk first; if the walk finds nothing **but** a row is `absent`-and-was-`after`, do not silently commit
— require an explicit operator acknowledgement (a flag, or a recorded retirement) instead of inferring
retirement from a failed search.

---

## 17. Reproduction index

All scripts under
`…/559b1f56-3e1e-46c8-8e0b-f48493da2094/scratchpad/`, run with `R9_BRIDGE_DIR` unset (candidate) or pointed at
`scratchpad/baseline-r8/` (round-8 baseline):

| file | covers |
|---|---|
| `envlib.py`, `cli.py`, `action.py`, `killer.py` | isolated fake-SSD harness; real `main()`; real `SIGKILL` |
| `scenario_a.py` | (a) R8-P1-A, incl. non-vacuity vs baseline |
| `scenario_bcde.py` | (b) 5 relocation shapes · (c) 3 deletion shapes · (d) 11 malformed payloads · (e) 5 permission shapes |
| `scenario_e4.py` | **R9-P1-B** minimal repro with the before/after `chmod` control |
| `scenario_h1.py` | **R9-P1-A**, both relocation variants |
| `scenario_h.py` | (h) race, wide-tree cost, symlink cycle, off-SSD symlink |
| `scenario_g.py`, `g6.py` | (g) rounds 1–7 spot-checks |
| `vac_probe.py`, `vac/` | (f) new-test non-vacuity; **R9-P3-C** |

## 18. What would have to change for GO

1. **Run the scan in the `kind == "install"` rollback branch too** (or, better, once at the top of
   `recover_pending_install()` before the `kind` split), so no journal commit of either direction can report
   success while an untracked owned handler is reachable. Without this, R9-P1-A stands.
2. **Split the scan's read step from `validate_owned_file()`.** Detection needs only
   `O_RDONLY|O_NOFOLLOW` + a size cap; "cannot read" (`EACCES`, `EIO`, identity change) must escalate to
   `InstallError`, while "not a regular file / not ours to identify" may still be skipped. Do **not** simply
   let the current `InstallError` propagate — the real tree already holds six unrelated `hooks.json` files,
   and a blanket fail-closed would hand any of them a permanent veto over `uninstall`.
3. **Write the precondition down and check it.** Add the `absent`-row-that-was-`after` check as a second,
   search-independent gate on every receipt-deleting commit, with an explicit operator-acknowledged retirement
   path so R3-P1-A stays fixed. Items 1–2 close the two doors I found; item 3 is what stops round 10 from
   finding a third.
4. Correct the R8-P2-B / R5-P3-A / R3-P3-B / R6-P2-B status to **partially fixed** (directory + `stat` halves
   closed and now interpreter-consistent; file-read half open on both).
5. Make `test_uninstall_ignores_untracked_configs_with_malformed_or_unexpected_content` non-vacuous by placing
   at least one variant inside `codex-accounts/<name>/home/`, and re-verify it fails on `37d2433a77`.
6. Optional but cheap: mention the resurrected-directory wrinkle (R9-P3-A) in the refusal message, since it
   silently blocks the remedy that message recommends.

Items 1–2 are blocking. Item 3 is the structural fix. Items 4–6 are not blocking; item 4's current claim is
inaccurate as written.
