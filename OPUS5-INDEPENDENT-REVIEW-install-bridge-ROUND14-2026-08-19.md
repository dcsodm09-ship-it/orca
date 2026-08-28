# Independent review — `claude-codex-memory-bridge/install_bridge.py`, round 14

**Reviewer:** Claude opus5 / effort `max`, read-only, independent
**Date:** 2026-08-19
**Candidate:** `f9a2d6605685bc16596ae49d9aa039a8a87802a5` — *"fix(install_bridge): close R13-P2-A — an oversized-and-unreadable RUNTIME_BASE candidate must stay tolerated, not hard-block"*
**Baselines used as controls:** `0ae1c0eaeadfcc3bf1d2d541acf5b6139b16d3d7` (round 13), `977dac84d4` (round 12, pre-streaming-scan), `366e703553` (round 11)
**Interpreters:** `/usr/bin/python3` 3.9.6 and PATH `python3` 3.14.6 (`/opt/homebrew/opt/python@3.14/bin/python3.14`)

---

## 0. Verdict

# **GO**

No P0 and no P1. Every claim in the dispatch brief that I could test, I tested from scratch and confirmed independently:

* **(a)** R13-P2-A is genuinely fixed. My own repro — different account names, a different subtree (`releases/`, not `backups/`), a different size, non-NUL content, a different permission mode (`0o200` write-only, not `0o000`), and the opposite planting order (before the first install, not after) — reproduces the regression exactly on the round-13 baseline and is fully tolerated on the candidate, at all three entry points, on both interpreters.
* **(b)** The round-13 fix (R12-P1-A / P2-R12-A) is completely intact and was **not** weakened. My own oversized live relocated handler still refuses at all three entry points, including a marker deliberately straddling the 1 MiB chunk boundary, a marker five chunks deep, and a whole-document UTF-16-LE encoding. Non-vacuity proven against the round-12 baseline, where all three silently succeed.
* **(c)** I enumerated every exception `_stream_scan_oversized_for_bridge_marker()` can raise and tested each. Outside-RUNTIME_BASE behavior and marker-hit-fails-closed are bit-for-bit unaffected. Real-`SIGKILL` crash recovery, in both journal directions, shows no new lockout and no new false success. **One genuine round-14 narrowing exists** (`R14-P3-A`, below) but is not reachable on the storage class this tool enforces; graded P3.
* **(d)** The new regression test is non-vacuous, and its blast radius is exactly one test.
* **(e)** 98/98 green, exit 0, on both interpreters.
* **(f)** 17/17 of my own independent regression checks across rounds 1–13 pass on both interpreters.

I also found **one unclaimed improvement** the same two-line change delivers (§4.6), and **two P3 nits** (§5).

Nothing in this review touched the real `/Volumes/Extreme SSD` Codex `hooks.json`. Every `install()`/`uninstall()`/`recover_pending_install()` call ran against a fake SSD inside a fresh `tempfile.TemporaryDirectory()`, or inside a `subprocess` pointed at one.

---

## 1. What I actually reviewed

The candidate changes exactly two things in `install_bridge.py`:

1. **`:918-938`** — the `try`/`except` around the streaming scan call inside `_find_untracked_owned_configs()`'s `_CandidateTooLargeForDetection` branch:

   ```diff
   -            except Exception as scan_exc:
   -                raise InstallError(f"{scan_exc} (at {candidate_str})") from scan_exc
   +            except Exception:
   +                ... # 17 lines of comment
   +                continue
   ```

2. **`:769-778`** — the invariant paragraph above the walk loop, amended to distinguish "genuinely unreadable" from "readable but oversized".

Plus one new test in `tests/test_install_bridge.py` (`:1305-1331`).

`git diff f9a2d66056 -- claude-codex-memory-bridge/` against the working tree is empty, so what I reviewed is the candidate commit verbatim, not a drifted tree. Candidate is an ancestor of `HEAD` (`6d81730d54`, a docs-only commit).

### Independence

I did not reuse the project's fixtures, the round-13 reports' examples, or the candidate's own regression tests for any primary verification. I built a fresh harness (`harness.py`) with:

| | candidate's own test | opus round-13 report | **this review** |
|---|---|---|---|
| accounts | `acct-one` (1) | `acct-quebec-77`, `acct-yankee-03` (2) | `workstation-delta-4419`, `mirror-sierra-88` (2) |
| pre-existing handler | `/usr/bin/true` | `/bin/echo pre-existing` | `/usr/bin/env printf legacy-audit-probe\n` |
| fake volume UUID | `AAAA…EEEE` | (own) | `7F3A21C4-9D0E-4B15-88AA-0C6E5D2F1A93` |
| (a) stray location | `backups/misc-staging/oversized-and-unreadable/` | `backups/2026-05-02T00-00-00Z/quarantine/` | `releases/2024-11-30T09-15-42Z/rollback-cache/staged/` |
| (a) stray size | `MAX+1` | `MAX+1` | `MAX+1_000_003` (≈5.0 MiB) |
| (a) stray content | `b"\x00" * n` | `b"\x00" * n` | repeated ASCII operator-log lines |
| (a) unreadable via | `chmod 0o000` | `chmod 0o000` | `chmod 0o200` (write-only) **and** `0o000` |
| (a) planting order | after `install()` | (n/a) | **before** the first `install()` |
| (b) padding shape | one long string sorting *before* `hooks` (marker near EOF) | JSON array of 100k strings sorting *after* `hooks` (marker near BOF) | padding split across `aaa_import_ledger` **and** `zzz_import_trailer` (marker **mid-file**, offset-tuned) |

Scripts (all local, in this session's scratchpad `…/4f9fe432-…/scratchpad/r14/`):
`harness.py`, `repro_a.py`, `repro_b.py`, `repro_c.py`, `repro_c1.py`, `repro_c2.py`, `repro_crash.py`, `repro_vanish.py`, `repro_f.py`, `probe_install_writes.py`, `probe_r2.py`, `probe_selfheal.py`.

---

## 2. (a) R13-P2-A is genuinely fixed

**Shape.** A `hooks.json` at
`RUNTIME_BASE/releases/2024-11-30T09-15-42Z/rollback-cache/staged/hooks.json`,
5,194,307 bytes (`MAX_MANAGED_FILE_BYTES + 1_000_003`), content = repeated
`2024-11-30T09:15:42Z rollback-cache staged operator note; no handler here.\n`,
mode `0o200`, planted **before** the first `install()`.

Three sub-cases, each run through `install()`, `uninstall()`, and `recover_pending_install()` on a genuine `kind: "uninstall"` journal:

* **A1** — oversized + unreadable (`0o200`), no marker in the bytes
* **A2** — oversized + unreadable (`0o200`), marker **is** in the bytes (since it cannot be read, it must *also* be tolerated: the tolerance decision is the read failure, not the content)
* **A3** — oversized + unreadable via `0o000` instead

I instrumented `_stream_scan_oversized_for_bridge_marker()` to prove the repro is not vacuous — that the walk really reaches the stray and the scan really runs and really raises:

```
[A1] planted …/rollback-cache/staged/hooks.json  st_size=5194307 mode=0o200
  [install() with stray present]   -> OK {'release_id': '562a6317…'}
  [  pending journal left?]        -> OK False
  [uninstall()]                    -> OK {'ok': True}
    scan InstallError: cannot read …/rollback-cache/staged/hooks.json     (×3)
  [recover_pending_install()]      -> OK {'ok': True, 'state': 'uninstalled'}
    scan InstallError: cannot read …/rollback-cache/staged/hooks.json     (×1)
  [  journal cleared?]             -> OK True
```

Identical on **3.14.6**. A2 and A3 identical.

**Control — the same repro against the round-13 baseline `0ae1c0ea`:**

```
[A1] install()                    -> InstallError: install failed and the durable
                                     recovery journal remains pending
     pending journal left?        -> True
     uninstall()                  -> InstallError: cannot read …/staged/hooks.json
                                     (at …/staged/hooks.json)
     bridge handler still live in a config? -> True
     recover_pending_install()    -> InstallError: cannot read …  (at …)
     journal cleared?             -> False
```

That is R13-P2-A precisely as the round-13 report describes it, reproduced from a completely different fixture, and it is the worst form: `install()` fails, leaves a durable pending journal, and leaves the bridge handler live in the configs — with every remediation path also blocked.

**(a) verified fixed, on both interpreters, all three entry points, all three sub-cases.**

---

## 3. (b) The round-13 fix is fully intact

Four fresh shapes, each planted at
`RUNTIME_BASE/backups/2023-07-04T18-02-11Z/import-scratch/mirrored-home/hooks.json`,
each a genuine, well-formed, **live** owned handler (real `--bridge-id …` command string) with padding split across a key sorting before `hooks` and one sorting after it:

| | size | encoding | marker offset | straddles a 1 MiB chunk boundary? | expected |
|---|---|---|---|---|---|
| **B1** | `MAX+1_052_818` | UTF-8 | 1,048,569 | **yes** (spans the chunk-0/1 boundary) | refuse |
| **B2** | `MAX+2_097_251` | UTF-8 | 3,500,000 (chunk 3) | no | refuse |
| **B3** | `MAX+6_292_062` | **UTF-16-LE** | 5,243,170 (chunk 5) | no | refuse |
| **B4** | `MAX+1_048_611` | UTF-8, **no marker** | — | — | tolerate |

Result on the candidate, **both interpreters**, all three entry points:

```
B1  uninstall: REFUSED   install: REFUSED   recover: REFUSED    PASS
B2  uninstall: REFUSED   install: REFUSED   recover: REFUSED    PASS
B3  uninstall: REFUSED   install: REFUSED   recover: REFUSED    PASS
B4  uninstall: proceeded install: proceeded recover: proceeded  PASS
```

with the correct, path-naming message:

```
InstallError: cannot rule out an owned hook handler: oversized content under
RUNTIME_BASE matches the bridge marker (at …/mirrored-home/hooks.json)
```

The instrumented scan returned `True` for B1–B3 and `False` for B4, so the refusals come from the streaming scan itself, not from some upstream check.

**Non-vacuity control — the same four shapes against the round-12 baseline `977dac84d4`:**

```
B1  uninstall: proceeded  install: proceeded  recover: proceeded   *** FAIL ***
B2  uninstall: proceeded  install: proceeded  recover: proceeded   *** FAIL ***
B3  uninstall: proceeded  install: proceeded  recover: proceeded   *** FAIL ***
B4  uninstall: proceeded  install: proceeded  recover: proceeded   PASS
```

i.e. exactly the original R12-P1-A silent abandonment. **(b) verified: intact, not weakened.**

Worth noting explicitly: B1 is the sharpest check of the interaction between the two rounds. The overlap window is the one piece of scan state that a widened `except` could plausibly have disturbed, and a marker sitting at byte 1,048,569 — seven bytes short of the chunk boundary, so it is split across two `os.read()` calls — still resolves to a hard refusal.

---

## 4. (c) Hunting for anything new

### 4.1 Complete enumeration of what `_stream_scan_oversized_for_bridge_marker()` can raise

Read directly from `:678-727`. Everything before the `try` (the `marker_variants` list comprehension, `max()`, the two integer literals) cannot fail.

| # | site | raised | now tolerated under RUNTIME_BASE? | correct? |
|---|---|---|---|---|
| 1 | `:707` `path.lstat()` | `OSError` → `InstallError("cannot read …")` | yes | **yes** — absence of evidence; this is R13-P2-A's own target |
| 2 | `:710` `os.open()` | `OSError` (EACCES / ENOENT / ELOOP / EMFILE) → `InstallError("cannot read …")` | yes | **yes** — same |
| 3 | `:711` `os.fstat()` | `OSError` → `InstallError("cannot read …")` | yes | **yes** — same |
| 4 | `:713` identity check | `InstallError("file identity changed while inspecting …")` — **not** an `OSError`, so `:723` does not wrap it; escapes as-is | yes | **yes, and consistent** — see §4.2 |
| 5 | `:716` `os.read()` | `OSError` (EIO) → `InstallError("cannot read …")` | yes | **yes** — same |
| 6 | `:719-720` `tail + chunk`, `variant in window` | `MemoryError` | yes | acceptable — bounded to ≤1 MiB + 51 bytes, so effectively unreachable; and it is still absence of evidence |
| 7 | `:727` `os.close()` **in the `finally`** | `OSError` propagates raw, and **can displace a pending `return True`** | yes | **this is the one real narrowing** — see §4.3 |

There is exactly one positive-evidence signal from this function — the `return True` at `:721` — and it is a *return value*, not a raise. So the widened `except` can only lose positive evidence through path 7, where the `finally` pre-empts the return. Everything else it now swallows is, by construction, an absence of information.

`BaseException` subclasses that are not `Exception` (`KeyboardInterrupt`, `SystemExit`) are **not** caught. Verified empirically:

```
[KeyboardInterrupt] -> KeyboardInterrupt  PASS (escaped)
[SystemExit]        -> SystemExit         PASS (escaped)
```

That matters: a `^C` during a real install must not be silently absorbed into "this candidate looks fine".

One further detail I checked because it is exactly the kind of thing that bites at this altitude: the new clause is a **bare** `except Exception:`, not `except Exception as exc:`. Had it bound `exc`, Python's implicit `del exc` at the end of the clause would have destroyed the outer `except _CandidateTooLargeForDetection as exc:` binding that `:943`'s `raise … from exc` still uses, turning every genuine marker hit into a `NameError`. The bare form is correct, and B1–B3's clean error messages confirm the chain still works.

### 4.2 Identity-changed-mid-scan is now tolerated again — consistent, not novel

**Isolated repro** (`repro_c1.py`): an always-racing variant where every `os.open()` of the candidate path first `os.rename()`s an identical-content shadow file into place — a *real* rename producing a real new inode — so the `lstat`/`fstat` identity check at `:712` can never succeed. The file at that path **always** holds a live owned handler, so a tolerate means a live untracked handler was passed over.

| case | round 12 | round 13 | **round 14 (candidate)** |
|---|---|---|---|
| oversized, **inside** RUNTIME_BASE | tolerated | **refused** | tolerated |
| small, **inside** RUNTIME_BASE | tolerated | tolerated | tolerated |
| oversized, **outside** RUNTIME_BASE | refused | refused | refused |
| small, **outside** RUNTIME_BASE | refused | refused | refused |

Reading the matrix: the small-file path has tolerated identity-change inside RUNTIME_BASE across **every** round in this cycle — it is the explicitly documented policy at `:895-897` ("*every other failure (cannot open/read, identity changed mid-read) is absence-of-evidence*"), reviewed and accepted repeatedly, including in two dual-GOs. Round 13 made the oversized path momentarily *stricter* than its small-file sibling, as an unintended side effect of the unconditional escalation. Round 14 realigns them.

So this is **not** a new hole. It is the pre-existing, documented RUNTIME_BASE absence-of-evidence policy being applied uniformly. Three further points make it a sound trade rather than a quiet regression:

1. The adversary required is same-uid or root under `RUNTIME_BASE` — the boundary this file's own comment at `:824-829` explicitly declares outside the scan's threat model.
2. Tolerance requires the race to **persist across every invocation**. I confirmed this: in my first, non-isolated version of the race (where the swap fired only once), the *second* pass through the scan — `uninstall()`'s own finalizing `recover_pending_install()` — caught the now-stable marker file and refused. A one-shot swap does not get you a durable bypass.
3. Outside RUNTIME_BASE, identity-change still fails closed in all three builds, unchanged.

Graded **P3, observation only**. If it were P1-worthy, the small-file path would already have been P1 for thirteen rounds.

### 4.3 `R14-P3-A` — the one genuine round-14 narrowing

**Finding.** If `os.close()` in `_stream_scan_oversized_for_bridge_marker()`'s `finally` (`:725-727`) raises **after** the marker was found, Python discards the pending `return True` at `:721` and propagates the `OSError` instead. The round-14 call site's `except Exception: continue` then swallows it, and the confirmed marker hit is silently dropped: `uninstall()` returns `ok: true`, deletes the receipt, and leaves a live untracked owned handler running.

Round 13 fail-closed on exactly this. Traced repro (`repro_c2.py`), oversized marker-bearing file, `EIO` injected into `os.close()` for the scan's own fd only:

```
                            module            outcome
round 12 (977dac84d4)   pre-streaming-scan    OK    (tolerated — the original R12-P1-A bug)
round 13 (0ae1c0eaea)   baseline              InstallError: [Errno 5] simulated close()
                                              failure (at …/mirror/hooks.json)   ← fail-closed
round 14 (f9a2d66056)   CANDIDATE             OK    ← *** positive evidence dropped ***

  trace open victim: 4
  trace close injected EIO: 4
  trace scan raised: OSError: [Errno 5] simulated close() failure
```

Same on both interpreters.

**Why this is P3 and not P1.** The *outcome shape* is this cycle's P1 shape — silent wrong success, the same family as R9-P1-B, R11-P1-B and R12-P1-A. It is graded P3 purely on reachability, and I want to be explicit that the grading rests on that and nothing else:

* `close(2)` on an `O_RDONLY` descriptor with no dirty pages does not fail on a local APFS volume. POSIX permits `EIO`/`EBADF`/`EINTR`; `EBADF` is impossible here (the fd is live and never double-closed), and CPython's `_Py_close` absorbs `EINTR` rather than raising.
* `resolve_ssd_path()` (`:135-159`) pins every candidate to the same `st_dev` as `SSD_ROOT`, so a network filesystem — where `close()` genuinely can surface deferred write errors — cannot host the candidate in the first place.
* My repro cannot trigger it without injecting a fabricated failure into `os.close`. Unlike R11-P1-B (`chmod 0664`) or R12-P1-A (padding a file), there is no operator action, tooling artifact, or crash that produces it.

It therefore does not block. It is, however, worth closing, because the fix is three lines, costs nothing, and removes the only path by which this function can convert a confirmed positive into silence:

```python
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
```

That is strictly safer than the alternative of re-narrowing the call site's `except`, which would reopen R13-P2-A. **Recommended as a round-15 follow-up, not a gate.**

*(For completeness: `_read_for_detection()` at `:265-269` has the structurally identical `finally`-close. A close failure there discards the returned bytes rather than a determined verdict, and it behaves identically in rounds 12, 13 and 14 — I confirmed this on r13 and r14. Pre-existing, unchanged by this round, and lower-severity since no verdict had been reached. Mentioned only so the enumeration is complete.)*

### 4.4 Outside RUNTIME_BASE — completely unaffected

The `if not _is_under_runtime_root(resolved): raise` guard at `:901-908` sits **before** the scan call, so nothing outside RUNTIME_BASE ever reaches the widened `except`. Confirmed by fresh construction rather than by reading — five combinations, planted at `local-homes/relocated-outside/adrift-home/hooks.json`:

```
[oversized + marker      ] -> REFUSED: cannot safely inspect …
[oversized + no marker   ] -> REFUSED: cannot safely inspect …
[oversized + unreadable  ] -> REFUSED: cannot safely inspect …
[small + unreadable      ] -> REFUSED: cannot read …
[small + live marker     ] -> REFUSED: refusing uninstall: found an owned hook
                                       handler at a path the current receipt
                                       does not track …
```

Identical on both interpreters, and identical to the round-13 baseline. **Unaffected.**

### 4.5 A tolerated stray must not mask a genuine hit

The new `continue` skips only the current candidate, but a walk-ordering bug here would be easy to miss, so I checked both orderings explicitly: an oversized-and-unreadable stray planted alongside a genuine oversized marker-bearing handler in the same `backups/` tree, once sorting **before** it (`aaa-quarantine` / `zzz-restored`) and once **after** (`aaa-restored` / `zzz-quarantine`).

Both orderings refuse, naming the correct path:

```
refusing uninstall: found an owned hook handler at a path the current receipt
does not track …: …/backups/zzz-restored/real-home/hooks.json
refusing uninstall: found an owned hook handler at a path the current receipt
does not track …: …/backups/aaa-restored/real-home/hooks.json
```

Both interpreters. **No masking.**

### 4.6 Unclaimed improvement: the vanish-mid-scan race is also fixed

Not mentioned in the commit message or the brief, but the same two-line change closes a second, arguably more realistic instance of the same regression: a candidate **deleted** between `_read_for_detection()`'s size check and the scan's own `open()` — a backup pruner, a restore tool cleaning up, an operator `rm`.

```
                        INSIDE RUNTIME_BASE            OUTSIDE
round 13 baseline   InstallError: cannot read …    InstallError: … too large
round 14 candidate  OK                             InstallError: … too large
```

A file that is *gone* is the canonical ENOENT case this function tolerates everywhere else (`:758-760`); round 13 hard-blocked on it. Now correct, and outside RUNTIME_BASE is unchanged. Worth recording so a future round does not mistake it for drift.

### 4.7 Real `SIGKILL` crash recovery

Real `SIGKILL` (`returncode == -9` asserted on every run), not a simulated in-process exception, delivered from inside `atomic_write` in a child subprocess, then recovery run in a **separate fresh subprocess**. I probed `install()`'s `atomic_write` ordering first (`probe_install_writes.py`) so the kill point is meaningful rather than accidental: kill #13 lands after the journal (#10) and after two of three configs were bridged (#11, #12) — a genuine mixed, first-ever-install crash state with no `latest-receipt.json`.

Both journal directions × three stray conditions:

| crash | stray | journal survived | recovery #1 | journal after | configs still bridged |
|---|---|---|---|---|---|
| mid-`uninstall` | none | yes (`kind: uninstall`) | `ok, state=uninstalled` | cleared | 0/3 |
| mid-`uninstall` | **oversized + unreadable** | yes | `ok, state=uninstalled` | cleared | **0/3** |
| mid-`uninstall` | oversized + **marker** | yes | **refused**, names the path | retained | 2/3 (crash state preserved) |
| mid-`install` | none | yes (`kind: install`) | `ok, state=rolled_back` | cleared | 0/3 |
| mid-`install` | **oversized + unreadable** | yes | `ok, state=rolled_back` | cleared | **0/3** |
| mid-`install` | oversized + **marker** | yes | **refused**, names the path | retained | 2/3 (crash state preserved) |

Both marker rows then **self-heal completely** once the stray is removed (`recovery #2` → `uninstalled` / `rolled_back`, journal cleared, 0 configs bridged). Verified with a 3.9.6 parent + 3.9.6 child *and* a 3.14.6 parent + 3.14.6 child.

**Non-vacuity control** — the same crash with the unreadable stray, against the round-13 baseline:

```
mid-uninstall + unreadable -> ERROR InstallError: cannot read …/sigkill-lab-2211/…
                              journal still pending: True; configs still bridged: 2/3
mid-install   + unreadable -> ERROR InstallError: cannot read …/sigkill-lab-2211/…
                              journal still pending: True; configs still bridged: 2/3
```

That is the worst realistic manifestation of R13-P2-A: a machine wedged mid-transaction after a crash, with recovery itself blocked. The candidate recovers cleanly from it. **No new lockout, no new false success.**

---

## 5. Nits (P3, non-blocking)

**`R14-P3-B` — the amended invariant comment is historically backwards.** `:769-772` now reads:

> `… an unreadable file, and (since round 13) an oversized-and-unreadable file all instead degrade to "nothing found there" …`

Round 13 is precisely when the oversized-and-unreadable case **stopped** degrading to "nothing found there" — that is what R13-P2-A *is*. The tolerance predates round 13 and was *restored* by round 14. As written, a reader debugging this in round 20 would date the tolerance to the round that broke it. In a file whose entire documentation discipline is exact round-and-finding attribution, and in a hunk whose stated purpose #2 was to make this very paragraph more precise, this is worth correcting. Suggested: `… an unreadable file, and an oversized-and-unreadable file (tolerated through round 12, hard-blocked by round 13's new streaming scan, restored in round 14 — R13-P2-A) all instead degrade to …`.

**`R14-P3-C` — comment wrapping.** `:778` is 96 characters; every other comment line in that block wraps at ≤76, and the edit leaves two adjacent parentheticals colliding mid-line (`… to distinguish the two) (self-check Workflow, 2026-08-18, round 2`). Cosmetic only.

---

## 6. (d) The new regression test is non-vacuous

`test_uninstall_tolerates_a_file_that_is_both_oversized_and_unreadable_inside_its_own_runtime_tree`, run against a hybrid tree (round-13 `install_bridge.py` + the candidate's test file):

```
install_bridge.InstallError: cannot read …/backups/misc-staging/oversized-and-unreadable/hooks.json
  File ".../install_bridge.py", line 913, in _find_untracked_owned_configs
    marker_found = _stream_scan_oversized_for_bridge_marker(resolved)
  File ".../install_bridge.py", line 724, in _stream_scan_oversized_for_bridge_marker
    raise InstallError(f"cannot read {path}") from exc
The above exception was the direct cause of the following exception:
  File ".../install_bridge.py", line 915, in _find_untracked_owned_configs
    raise InstallError(f"{scan_exc} (at {candidate_str})") from scan_exc
install_bridge.InstallError: cannot read … (at …)
FAILED (errors=1)
```

Errors, for exactly the right reason, at exactly the removed call site (`:915` of the baseline), with exactly the bare `cannot read … (at …)` message the round-13 report describes. Passes on the candidate on **both** interpreters (`Ran 1 test … OK`).

**Blast radius.** Running the *whole* candidate test file against the round-13 baseline:

```
Ran 59 tests in 5.764s
FAILED (errors=1)
  test_uninstall_tolerates_a_file_that_is_both_oversized_and_unreadable_inside_its_own_runtime_tree ... ERROR
```

Exactly one test changes behavior — the new one. The 58 pre-existing tests, including both round-13 streaming-scan tests and both existing single-half tolerance tests, are untouched. That is the strongest available evidence that the fix changes precisely the one behavior it claims to and nothing else.

Method counts confirm the brief: `test_install_bridge.py` **59**, `test_claude_memory_hook.py` **39**, total **98**.

---

## 7. (e) Full suite

```
### /usr/bin/python3 (Python 3.9.6) ###
  exit=0
Ran 98 tests in 5.476s
OK
  ok-count: 98   fail/err-count: 0

### python3 (Python 3.14.6) ###
  exit=0
Ran 98 tests in 8.198s
OK
  ok-count: 98   fail/err-count: 0
```

Real green on both, verified by exit status *and* by counting `... ok` lines, not by trusting the summary alone.

---

## 8. (f) Rounds 1–13 regression spot-check

My own fixtures and relocation shapes, not the repo's tests. **17/17 PASS on both interpreters.**

| round / finding | property checked | result |
|---|---|---|
| R1 `P1-1` | a second install must not adopt the already-bridged state as pristine | PASS — uninstall restores byte-identical pristine |
| R2 `P1-R2-1` | park-out-of-tree then restore keeps the real baseline (carry-forward) | PASS |
| R6-P1-A / R7-P1-A | an in-tree relocation refuses on `install`/`uninstall`/`recover`, `plan()` still works, and it **fully self-heals** once the path is restored | PASS (`refusals=[True,True,True] plan_ok=True healed=True`, pristine restored) |
| R3-P1-A | a genuinely deleted account does not lock out uninstall | PASS |
| R8-P1-B | relocation into a subfolder / out of `codex-accounts` / with `home/` renamed | PASS ×3 — all refused |
| R8-P2-B | unlistable directory **outside** RUNTIME_BASE escalates (`cannot list …`) | PASS |
| R9-P1-B | a relocated live handler at `0664` is still detected | PASS |
| R10 / R11 encoding bypass | UTF-16-LE / UTF-8 BOM / duplicate top-level `hooks` key / trailing comment text | PASS ×4 — all caught |
| R11-P1-A | 4301-digit integer (CPython int↔str cap), inside **and** outside RUNTIME_BASE | PASS ×2 — no bare exception on 3.14.6, where the limit exists |
| **R11-P1-B** | a 262,447-byte valid handler in the band above `_STRUCTURAL_DETECTION_MAX_BYTES` but under `MAX`, under RUNTIME_BASE | PASS — refused |
| **R11-P2-A** | listable-but-not-searchable directory under RUNTIME_BASE tolerated | PASS |
| **R12-P1-A / P2-R12-A / round 13** | oversized relocated live handler under RUNTIME_BASE | PASS — §3, four shapes, three entry points |

All three round-11 findings and the round-12/13 oversized-relocated-handler fix specifically re-verified, as the brief asked.

*One methodology note, in case it is useful for calibration:* my first `R2/P1-R2-1` check failed, and the failure was **mine, not the product's**. I had parked the account by renaming `home/` → `home-parked/` *inside* the walked tree, which rounds 7–9 deliberately refuse (a relocated live handler must block). I confirmed the diagnosis directly (`probe_r2.py`), rewrote the check to park the account outside `local-homes` — the genuinely-undiscoverable case round 2's carry-forward fix is actually about — and it passes. I then added the in-tree case back as its own R6/R7 self-heal check, which also passes. Recording this because a less careful reviewer could easily have filed that first red as a P1 regression.

---

## 9. Summary of findings

| id | severity | status | summary |
|---|---|---|---|
| `R13-P2-A` | P2 (round 13) | **CLOSED — independently verified** | oversized-and-unreadable RUNTIME_BASE candidate no longer hard-blocks `uninstall()`/`install()`/`recover_pending_install()` |
| `R14-P3-A` | **P3** | new, non-blocking | `os.close()` failing in the scan's `finally` after a marker hit discards the confirmed positive; round 13 fail-closed here. Not reachable on the local-APFS storage class `resolve_ssd_path()` enforces. 3-line hardening in §4.3 |
| `R14-P3-B` | P3 | new, non-blocking | the amended invariant comment's `(since round 13)` dates the oversized-and-unreadable tolerance to the round that broke it |
| `R14-P3-C` | P3 | new, cosmetic | `:778` is 96 chars in a block that wraps at ≤76 |
| identity-changed-mid-scan | P3 | observation, not a defect | now tolerated again on the oversized path; matches the small-file path's long-standing documented policy in every round. Not novel |
| vanish-mid-scan (ENOENT) | — | **unclaimed improvement** | round 13 hard-blocked on a candidate deleted mid-scan; now correctly tolerated |

**No P0. No P1. Verdict: GO.**

### Recommended follow-ups (round 15, none gating)

1. `R14-P3-A` — wrap the `finally`'s `os.close()` in `try: … except OSError: pass` so a close failure can never displace a determined `return True`. Three lines, no behavior change on any reachable path.
2. `R14-P3-B` / `R14-P3-C` — correct the `(since round 13)` attribution and re-wrap `:778`.
3. Carried forward from the round-13 report and still open: `_STRUCTURAL_DETECTION_MAX_BYTES`'s comment (`:425-438`) still claims a file over the bound "*skips straight to Layer 2's byte-pattern marker check, which stays cheap regardless of size*". Under RUNTIME_BASE that has been untrue since round 13, which makes an over-`MAX` file cost a full sequential read while holding the exclusive installer lock. Round 14 does not change that cost and does not make it worse — an *unreadable* oversized file now fails fast at `open()` instead of after a full read — but the comment is still stale.
