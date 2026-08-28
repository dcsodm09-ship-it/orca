# Independent Review — Trigger False-Positive Fixes + `install-write-trigger`

**Reviewer:** Claude opus5 / max, independent read-only leg
**Date:** 2026-08-20
**Candidate:** `完善orca/claude-codex-memory-bridge/` — working tree, uncommitted
**Interpreter:** `/usr/bin/python3` → Python 3.9.6 (Xcode shim), *not* Homebrew 3.14.6
**Parallel leg:** an independent Codex review ran on the same candidate; no coordination.

> **This review does not authorize installing anything.** Even the parts that pass
> below remain NOT installed. `install-write-trigger` must not be run without the
> human's own separate, explicit go-ahead after reading this.

---

## Verdicts

| Piece | Verdict | Blocking |
|---|---|---|
| **1 — trigger false-positive fixes** (`write_candidate_capture.py`) | **NO-GO** | P1-A, P1-B |
| **2 — `install-write-trigger`** (`install_bridge.py`) | **NO-GO** | **P0-1** |

Piece 2's P0 is a hard blocker: the capability **cannot function at all** as wired.
Piece 1 has no P0 and is a genuine net improvement with **zero measured over-blocking**;
its two P1s are well-scoped and individually small.

---

## Test suite (item 5)

```
/usr/bin/python3 -m unittest discover -s tests -v
Ran 274 tests in 12.138s
OK          (exit 0, 0 failures, 0 errors, 0 skips)
```
Confirmed `sys.version` = `3.9.6`. The 6 lines matching `skipped|ERROR|FAIL` are all
test *names* containing the word "skipped", not outcomes. Claim verified.

## `git status --short` (item 6)

Scoped to `claude-codex-memory-bridge/`, exactly as expected:

```
 M claude_memory_hook.py          <- prior (already-installed) redaction round
 M install_bridge.py              <- Piece 2
 M tests/test_claude_memory_hook.py <- prior round
 M tests/test_install_bridge.py   <- Piece 2
?? AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md
?? write_candidate_capture.py     <- Piece 1 (UNTRACKED, so `git diff` shows nothing)
?? tests/test_write_candidate_capture.py
?? tests/test_nontrigger_corpus.py   <- pre-existing, not in the task's expected list
```

Two notes: (a) `write_candidate_capture.py` and its test file are **untracked**, so the
task's suggested `git diff` command returns empty for Piece 1 — reviewed by reading the
files whole. (b) `tests/test_nontrigger_corpus.py` (511 lines) is present but was not
named in the expected set; it predates this round. `prime-agent-integration/` and
`round13-review-repros/` ignored as instructed.

---

# PIECE 2 — `install-write-trigger`

## P0-1 — The registered SessionEnd hook can never do anything. Permanent silent no-op.

**Severity: P0. Blocking.** The entire capability is inert in production.

### Chain

1. `make_release()` (`install_bridge.py:1354-1366`) builds the command as
   `/usr/bin/python3 <release_dir>/write_candidate_capture.py scan --bridge-id … --policy <release_dir>/policy.json …`
   — **no `--write-candidates-root`**.
2. `write_runtime()` (`install_bridge.py:1382-1396`) copies exactly three files into
   `release_dir`: `claude_memory_hook.py`, `policy.json`, `write_candidate_capture.py`.
   **`install_bridge.py` is not among them.**
3. `_main_scan()` (`write_candidate_capture.py:2556-2562`) calls `scan()` **without**
   `write_candidates_root`.
4. `scan()` (`write_candidate_capture.py:2408`) → `write_candidates_root or default_write_candidates_root()`.
5. `default_write_candidates_root()` (`write_candidate_capture.py:322-338`) → `import install_bridge as installer`.
6. The hook process's `sys.path[0]` is the script's own directory (`release_dir`), and the
   module additionally does `sys.path.insert(0, Path(__file__).resolve().parent)` — the same
   `release_dir`. `install_bridge.py` is on neither, nor on any site-packages path.
7. `ModuleNotFoundError` → swallowed by `scan()`'s first `except Exception: return None`
   (`write_candidate_capture.py:2426`) → `_main_scan` returns 0.

**Net effect:** the hook fires on every Codex session end, exits 0, prints nothing to
stdout or stderr, and writes nothing. Forever. There is no error surface anywhere, and
`verify()` still reports `ok: true` because it only hash-pins the file and counts
handlers — it never executes it.

### Reproduced end-to-end (my own harness, not the authors' isolated-revert claims)

Built an isolated fake SSD under the scratchpad (which is physically on
`/Volumes/Extreme SSD`, so device/residency checks are real), copied the **real**
`claude_memory_hook.py` and `write_candidate_capture.py` in as the release sources, ran
`install_write_trigger()` for real, then executed the **actual registered command string**:

```
release dir contents: ['claude_memory_hook.py', 'policy.json', 'write_candidate_capture.py']

$ python3 -c "sys.path.insert(0,<release_dir>); import write_candidate_capture as w;
              w.default_write_candidates_root()"      # cwd=/
ModuleNotFoundError: No module named 'install_bridge'
  at .../releases/<id>/write_candidate_capture.py:336 in default_write_candidates_root

$ <the exact registered SessionEnd command>  <<< '{"hook_event_name":"SessionEnd", …}'
rc=0   stdout=b''   stderr=b''
write-candidates/ exists: False
```

**Proof the rest of the pipeline is sound** — identical setup, but `write_candidates_root`
supplied explicitly and the volume-UUID reader stubbed (my temp root is not a mountpoint,
so `diskutil` fails there; irrelevant in production where `ssd_root` *is* the mountpoint):

```
turns: 2
  role='assistant' is_meta=False origin_kind=None  'I switched deploy to staging-west.'
  role='user'      is_meta=False origin_kind='human' "No, that's wrong -- we always deploy to staging-east…"
matches: [('T1', "Correction: No, that's wrong -- we always deploy to staging-east…")]
append_candidates -> written: 1  flagged: 0
pending.jsonl: {"candidate_id": "…", "trigger": "T1", "importance": 4, "kind": "feedback", …}
```

So this one import is the **sole** blocker. Everything downstream is correct.

### Why 274 tests cannot catch it

- All 12 `write_candidates_root=` call sites in `tests/test_write_candidate_capture.py`
  pass it **explicitly**. `default_write_candidates_root` appears in **zero** tests
  (`grep -rn default_write_candidates_root tests/` → no matches).
- Every `install_bridge` test calls `install_write_trigger()` **in-process**, where
  `install_bridge` is trivially importable because it is the module under test.
- No test ever executes the constructed command string as a subprocess from the release dir.

The design doc (§ line 204) records that an earlier round deliberately moved
`default_write_candidates_root()`'s `ImportError` inside `scan()`'s fail-closed boundary,
treating it as a defensive edge case. Piece 2 converts that edge case into the
100%-of-the-time production path.

### Fix options

- **(c) preferred** — remove the dependency entirely: derive the root from the policy's own
  `runtime_root`, which is `RUNTIME_BASE/"releases"/<id>` by construction, so
  `Path(policy["runtime_root"]).parent.parent / "write-candidates"` is exactly
  `default_write_candidates_root()` with no import.
- (a) add `--write-candidates-root` to the command — note `_main_scan` hard-requires
  **exactly 8** argv items (`len(argv) != 8` → fail closed), so that parser must change too.
- (b) copy `install_bridge.py` into the release dir as a 4th hash-pinned file.

**Whichever is chosen, the regression test must execute the real command string in a
subprocess from the release directory** — that is the only shape that would have caught this.

---

## P2-1 — `install-write-trigger` is not purely additive to the live read-side

Because `release_id` is content-addressed and now folds in `write_trigger`
(`install_bridge.py:1287-1289`), running this action produces a **new** `release_id`, and
`install()` rewrites the live `UserPromptSubmit` handler's command to point at the new
release directory. The `claude_memory_hook.py` bytes are re-copied identically, so behavior
is unchanged — but the human should know this action **rewrites the currently-live
read-side handler**, not merely appends a SessionEnd one. The previous release directory is
retained on disk with nothing pointing at it.

Verified: base release `483ebe035e37…` → write-trigger release `4e7528adfeef…`.

## P3-1 — `_load_write_trigger_config`'s `import write_candidate_capture` is unwrapped

`install_bridge.py:2415-2427`. If `write_candidate_capture.py` is not a sibling of
`install_bridge.py`, `main()` (which catches only `InstallError`) emits a raw traceback and
exit 1 instead of the `{"ok": false, "error": …}` contract every other failure honours —
the same class of gap the `atomic_write` fix in this file already closed. Ordinary failures
*do* honour the contract: `{"ok": false, "error": "cannot read write-trigger policy: /nonexistent.json"}`.

---

## Piece 2 — everything else verified correct

All of the following are **my own constructions**, not acceptance of the authors' claims.

### Release-key collision fix is genuinely load-bearing
I wrote my own revert — recomputing `release_key` from `{script_sha256, volume_uuid,
source_root, limits}` only and forcing the resulting `release_id`/paths — then ran base
`install()` followed by `install_write_trigger()`:

```
InstallError: immutable runtime collision: …/releases/91cfa19dc45c…/policy.json
```
With the fix in place the upgrade succeeds and yields a distinct `release_id`. Confirmed
load-bearing, not speculative.

### Fail-closed policy gate — 10 independent constructions, all refuse, all leave 0 handlers

| construction | result |
|---|---|
| `enabled: false` | `InstallError: write_trigger.enabled must be true…` |
| `write_trigger` key absent (`{}`) | same |
| `write_trigger: null` | same |
| malformed JSON (`{not json`) | `InstallError: invalid hook JSON` |
| non-object JSON (`[1,2,3]`) | `InstallError: … must be a JSON object` |
| `enabled: "true"` (string) | `InstallError: … must be a boolean` |
| `max_candidate_bytes: 10**9` | `InstallError: … out of range` |
| extra key inside `write_trigger` | `InstallError: unexpected write_trigger keys` |
| nonexistent path | `InstallError: cannot read write-trigger policy` |
| path is a directory | `InstallError: cannot read write-trigger policy` |

In every case `hooks.json` retained **zero** SessionEnd handlers.

### No TOCTOU and no argument-order bypass
`install_write_trigger()` (`install_bridge.py:2444-2457`) calls `_load_write_trigger_config()`
unconditionally before `install()`/`plan()`. More fundamentally the bypass is *structurally*
impossible: the supplied file is **never installed and never re-read**. `make_release()`
synthesizes a fresh `policy.json` with `write_trigger.enabled` **hardcoded to `True`**
(`install_bridge.py:1311-1318`), consuming only two already-range-checked integers from the
user's file. Swapping or deleting the file after validation changes nothing. `main()`
(`install_bridge.py:2543-2545`) requires `--write-trigger-policy` before dispatching, and no
other action path can reach `install(write_trigger=…)`.

### `--dry-run` genuinely writes nothing
Full recursive sha256 snapshot of the entire fake SSD root before and after:
**0 files added, 0 changed, 0 removed.** `plan()` never calls `recover_pending_install()`,
and `main()` deliberately skips the lock for dry-run — confirmed a dry-run succeeds while
another invocation holds the installer lock. Returns `action: "plan"` plus
`write_trigger_script_sha256`. No `__pycache__` side effect observed in the source dir.

### Command ↔ argparse integration is exact
`_main_scan` requires exactly 8 argv items forming four `--flag value` pairs drawn from
`{--bridge-id, --policy, --expected-policy-sha256, --expected-script-sha256}`, and returns 0
early unless `--bridge-id == MODULE_ID`. The constructed command matches on all counts;
`write_trigger["bridge_id"]` is `write_candidate_capture.MODULE_ID`. Verified by executing it.

### Transactional machinery
| check | result |
|---|---|
| `timeout` key omitted entirely on the SessionEnd handler | PASS |
| idempotent (2nd run byte-identical) | PASS |
| exactly 1 UserPromptSubmit + 1 SessionEnd handler after 2 runs | PASS |
| `verify()` ok, reports `write_trigger_script_sha256` | PASS |
| `uninstall()` restores pristine bytes (both handlers gone in one restore) | PASS |
| pre-existing **foreign** SessionEnd handler preserved on install | PASS |
| foreign handler byte-identical after uninstall | PASS |
| malformed pre-existing `SessionEnd` value → `InstallError`, nothing written | PASS |
| relocated managed config → `uninstall()` refuses | PASS |
| injected mid-install write failure → `recover_pending_install()` restores pristine | PASS |

On the relocation case: `_find_untracked_owned_configs()` is called with the default
`event=UserPromptSubmit`/`bridge_id=BRIDGE_ID`, but this is **safe** because both handlers
always land in the same file in a single write (`update_hook_config` is layered onto the
same payload, `install_bridge.py:1914-1923`), so the UserPromptSubmit-only scan still
catches any relocated config. Also confirmed `owned_handler(handler, bridge_id=None)`
returns `False` rather than raising.

---

# PIECE 1 — trigger false-positive fixes

## Empirical baseline

Surveyed the real in-scope corpus with the module's **own** `_parse_transcript_line`:
**325 main session files**, 7,219 filtered user turns.

**Scoping fact that governs several findings:** `_SESSION_FILE_RE`
(`write_candidate_capture.py:575-577`) matches only `<uuid>.jsonl`, and
`_list_session_files` uses a **non-recursive** `project_dir.iterdir()`. The 5,314
subagent transcripts under `<uuid>/subagents/agent-*.jsonl` are therefore **entirely out of
scope** — doubly so, since their records' `sessionId` also would not match the filename stem.
Confirmed: including or excluding them changes the measured numbers by zero.

`origin.kind` over in-scope filtered user turns:

| value | count |
|---|---|
| `human` | 2,928 |
| *(absent)* | 2,627 |
| `task-notification` | 1,589 |
| `peer` | 71 |
| `auto-continuation` | 4 |
| `isMeta: true` | 2,317 |

`_SYSTEM_ORIGIN_KINDS` = `{task-notification, peer, auto-continuation}` covers every
non-`human` value that actually occurs in scope. **The set is complete for the current scope.**

### G1b effectiveness — measured

| stage | count |
|---|---|
| user turns matching `_CORRECTION_RE` (filtered list) | 1,245 |
| …preceded by an assistant turn (reaches `_match_t1_t2`'s precondition) | **892 (71.6%)** |
| …**blocked by G1b** | **799 (89.6%)** |
| …surviving | 93 |
| — of survivors: **compaction/resume summaries (machine)** | **61 (65.6%)** |
| — of survivors: genuine human corrections | 32 (all `origin.kind == "human"`) |

**Over-blocking: zero.** All 32 genuine human corrections survive — visibly real
instructions (`'请改成导航页'`, `'不对啊要oci资料'`, `'原来是wg现在改成tunnel'`, …).
The gate works and costs nothing in true positives.

> **Correction to a claim you may see from the parallel/background survey:** a
> raw-JSONL-line-adjacency measurement suggests only ~3.8% of correction turns are
> preceded by an assistant, implying a broken precondition. **That does not apply to this
> code.** `_parse_transcript_line` returns `None` for every record whose
> `type` is not `user`/`assistant`, so all ~21 bookkeeping record types (`attachment`,
> `queue-operation`, `system`, `mode`, …) are dropped **before** `_match_t1_t2` ever sees the
> list. Measured on the filtered list the code actually operates on, the figure is
> **71.6%**. There is no adjacency bug.

---

## P1-A — Compaction/resume summaries are an uncovered system-injected shape. 61 live false positives.

**Severity: P1.** Two-thirds of everything that still gets through T1 is a false positive
from a single, trivially-detectable shape.

Claude Code's context-compaction summary is injected as a `role: "user"` record carrying
**`isMeta` absent, `origin` absent**, and beginning with the fixed literal
`This session is being continued from a previous conversation that ran out of context.`
It therefore passes all three G1b checks.

These records are 26–31 KB and are **summaries of past human corrections**, so they
reliably contain correction language — they are essentially *guaranteed* to keep matching
`_CORRECTION_RE`. Confirmed instances include
`0c13ba48-aa3d-498c-801a-f8afd2a65848.jsonl:2095` and
`45065a76-d0d0-445e-8d24-f77a7340de5c.jsonl:2379` (both `isMeta=False`, `origin_kind=None`).

This is exactly the failure class Finding 1 exists to close, and it is the single largest
remaining contributor. One additional literal — the same style as the dispatch-preamble
marker already there — takes the residual false-positive count in this corpus from **61 to 0**:

```python
_COMPACTION_RESUME_MARKER = "This session is being continued from a previous conversation"
# in _gate_g1b_system_injected_envelope:
if turn.text.lstrip().startswith(_COMPACTION_RESUME_MARKER):
    return True
```

Anchoring with `startswith` (not a substring test) avoids the over-block shape in P3-A.

---

## P1-B — The cross-project registry is a machine-wide poison point with no self-heal and zero test coverage

**Severity: P1.**

`load_cross_project_registry()` (`write_candidate_capture.py:2299-2346`) raises
`WriteCaptureError` on **any** of: non-regular / symlink / wrong-owner file; *any* group or
other permission bit (`info.st_mode & 0o077`); size > 4 MiB; unparseable JSON; or a schema
string that isn't exactly `CROSS_PROJECT_REGISTRY_SCHEMA`. `scan()` calls it inside the
block whose handler is `except Exception: return None`.

Before this round every durable state file was **per-project**
(`<project_ref>/checkpoint.json`, `<project_ref>/pending.jsonl`), so a poisoned file
disabled scanning for exactly one project. This registry lives at
`write_candidates_root/cross-project-fact-fingerprints.json` — **shared by every project on
the machine**. One stray `chmod`, one manual inspection with a tool that rewrites the file,
one schema bump or version downgrade, and **write-trigger scanning stops for every project,
permanently and silently** (the hook always exits 0 and prints nothing).

Reproduced all three poison vectors:
```
mode 0o644  -> WriteCaptureError: cross-project fact registry is not private: …
>4 MiB      -> WriteCaptureError: cross-project fact registry exceeds bound: …
schema v2   -> WriteCaptureError: unexpected cross-project fact registry schema
```

**Aggravating factor: the persistence layer has no tests at all.** `grep` for
`load_cross_project_registry|save_cross_project_registry` in
`tests/test_write_candidate_capture.py` returns **one comment** (line 1566) and **zero
tests**. The two registry tests (lines 632, 681) exercise the in-memory
`_register_and_check_cross_project_recurrence` path via `build_candidates`, never the file.
Every failure mode above is untested code.

**Recommended fix:** treat an unreadable / corrupt / unknown-schema registry as an **empty**
registry and continue. The registry is a purely *negative* heuristic — losing it degrades
to the pre-fix behaviour (more T3 false positives), which is strictly better than killing
the whole feature machine-wide. Contrast `_ensure_private_dir`, which self-heals.

---

## P2-A — `load_cross_project_registry` never re-applies the 500-entry cap

Eviction happens **only at insert time**
(`_register_and_check_cross_project_recurrence`, `write_candidate_capture.py:1657-1668`).
A file carrying more than `MAX_CROSS_PROJECT_REGISTRY_ENTRIES` entries — hand-edited,
restored from backup, or written by a future version with a higher cap — loads in full and
stays over-cap **forever**, since each subsequent insert evicts exactly one and adds one.

Reproduced: a 5,000-entry file loads all 5,000 (cap is 500). Bounded in practice only by
the 4 MiB read guard, i.e. tens of thousands of entries. Fix: prune after load.

## P2-B — "oldest-first FIFO" eviction is not FIFO after any reload

`_evict_oldest_cross_project_entry` (`write_candidate_capture.py:1638-1645`) relies on
Python dict insertion order, but `save_cross_project_registry`
(`write_candidate_capture.py:2354`) serializes with `sort_keys=True`. After any save/load
round trip the order is **lexicographic by fingerprint hash** — effectively random. Since
every SessionEnd scan loads and saves, insertion order is reset on **every invocation**, so
FIFO essentially never holds.

Reproduced: `['zzz','mmm','aaa']` → `['aaa','mmm','zzz']`.

Impact: eviction picks an arbitrary entry, so a long-lived genuine boilerplate fingerprint
can be evicted in favour of a one-off, weakening the heuristic. Not a safety issue.
(`checkpoint.working_set` shares the round trip, but `_evict_oldest_unpromoted` prefers
unpromoted entries so it degrades far less; this registry's eviction is *pure* FIFO and so
degrades fully.) Fix: store an explicit monotonic counter per entry.

## P2-C — G1b is not applied to the T3/T5 fact-sentence path

`build_candidates` (`write_candidate_capture.py:1476-1500`) runs
`_candidate_fact_sentences(turn)` and `_failure_signatures(turn)` over **every** turn,
gated only by `_apply_gates` (G1–G4). `_gate_g1b_system_injected_envelope` is invoked
**only** from `_match_t1_t2` (`write_candidate_capture.py:1251`).

So a `role: "user"` turn that *is* a system envelope still contributes T3 fact fingerprints
to both `checkpoint.working_set` and the new shared registry. Reproduced — a
`task-notification` turn yielded 2 fact sentences (`"I am the release coordinator for this
repo."`, `"We always tag releases with a v prefix."`) while the gate confirms it *would*
have rejected that same turn.

Two harms: (a) residual T3 false positives the cheap high-confidence metadata signal would
remove for free; (b) worse — envelope-derived fingerprints **pollute the shared
cross-project registry**, consuming its 500-entry budget and evicting genuine signal. The
registry only fires at ≥2 projects, so a system envelope recurring within *one* project
across 2 sessions still promotes.

## P2-D — Cross-file turn pairing produces corrupt evidence pointers *(pre-existing)*

`read_transcript_turns_since` flattens turns from **all** session files into one list
(`write_candidate_capture.py:978, 1007, 1043`), and `_match_t1_t2` takes `turns[index - 1]`
with **no `session_file` equality check**. The last turn of file A can therefore be paired
with the first turn of file B:

```
cross-file pairing produced a match: True
  evidence -> session_file=fileB.jsonl  line_start=999  line_end=0
  content: "Correction: No, that's wrong, use plan B.\nPrior assistant action: I will use plan A."
```

The emitted evidence range is inverted and points at a line index belonging to a *different*
file. This predates the round, but it lives in the exact function the round modified, so
flagging it here. Fix: `if previous.session_file != current.session_file: return None`.

## P3-A — The dispatch-preamble marker is an unanchored substring match

`write_candidate_capture.py:1184` — `return _DISPATCHED_WORKER_ONBOARDING_MARKER in turn.text`.
A genuine human correction that *quotes* the worker preamble for context is silently
dropped (reproduced synthetically). **Zero real occurrences in the corpus**, so P3 rather
than P2 — but it is a realistic shape in this very workspace, and `startswith` on the
lstripped text would close it at no cost.

## P3-B — A second, shorter preamble constant is not covered

`/Volumes/Extreme SSD/Orca/workspaces/orca/自动学习/src/shared/orca-dispatch-status-prompt.ts:8-9`
defines `ORCA_DISPATCH_STATUS_PREAMBLE_PREFIX = 'You are working inside Orca, a multi-agent IDE.'`
(47 chars). `compactDispatchPromptForStatus()` rebuilds the status preview from this prefix
and **drops** `You are a dispatched worker.`, so the compacted form evades the 76-char
literal. Zero in-scope occurrences today, hence P3.

**The 76-char literal itself is byte-exact** against
`src/main/runtime/orchestration/preamble.ts:60` — verified character-for-character; the
first line of that template literal contains no `${}` interpolation (interpolation starts
on line 61). Keying on the 47-char prefix instead would cover both forms.

## P3-C — `coordinator` origin.kind exists, but only outside the current file scope

117 records carry `origin.kind: "coordinator"` (47 of them already matching the correction
regex), all in `agent-*.jsonl` subagent transcripts — **0 in scope** today, because
`_SESSION_FILE_RE` and the non-recursive `iterdir()` exclude those files entirely. Same for
the `<fork-boilerplate>` worker-fork envelope (19 records). Both currently carry
`isMeta: true` incidentally. **If the module's file scope is ever widened to subagent
transcripts, `coordinator` must be added to `_SYSTEM_ORIGIN_KINDS` and the fork-boilerplate
shape given a marker** — otherwise both become immediate false-positive sources.

## P3-D — Checkpoint is saved before the registry (partial-commit window)

`scan()` saves the checkpoint (`write_candidate_capture.py:2482`) **before** the registry
(`:2484`). If the registry write fails, the checkpoint has already advanced past those
turns, so those T3 occurrences never reach the registry — cross-project evidence silently
lost. Swapping the order is strictly better: re-registering a fingerprint is idempotent,
re-scanning turns is not.

---

## Piece 1 — verified correct

- Metadata reads (`_parse_transcript_line:766-780`) tolerate dict-`origin`, bare-string
  `origin`, and absent `origin`; never raise. Empirically: 0 bare-string and 0 dict-without-`kind`
  records exist, so the tolerance is belt-and-braces, and correct.
- `is_meta`/`origin_kind` default to "genuinely human", so every existing construction site
  (tests included) is unchanged — confirmed a correction with no `origin` still matches T1.
- G1b short-circuits to `False` for `role == "assistant"` (`:1180`), so T4/T6 — which
  deliberately match assistant turns — are untouched.
- **Concurrency is genuinely safe.** The registry is loaded (`:2470`) and saved (`:2484`)
  strictly inside the process-wide `flock` acquired at `:2440`. `_lock_path` resolves to
  `write_candidates_root.parent / "installer.lock"` — the identical file
  `install_bridge._acquire_exclusive_lock()` locks — so concurrent installs and concurrent
  scans across *different* projects serialize against each other. The lock is
  `LOCK_EX | LOCK_NB`, so a losing concurrent scan raises → caught → `return None` with the
  checkpoint **not** advanced: nothing is lost, only deferred to the next scan. No new
  locking mechanism was introduced and none is needed.
- Registry samples are redacted on write **and** re-redacted on reload
  (`hook.redact(...)[:200]`), matching the checkpoint's treatment.
- Bounds hold under normal operation: 500 entries (1,200 inserts → 500), 8 projects/entry
  (30 distinct projects → 8).
- Exclusion semantics correct: same project twice never self-trips; a second distinct
  project trips it; T5 is deliberately not registered.
- The documented forward-looking limitation is real and reproduced: the **first** project to
  reach 2 sessions still emits the candidate, because cross-project evidence does not yet
  exist at its promotion moment. Accurately described in the code comment.
- The two fixture bugs are real and correctly characterised — `Path.mkdir(parents=True)`
  does not apply `mode` to intermediate parents, which `save_cross_project_registry` exposed
  by being the first code path to validate `write_candidates_root`'s own mode.

---

# Cross-piece invariants (item 4)

### MEMORY.md is never written — CONFIRMED
All 6 `MEMORY.md` occurrences in `write_candidate_capture.py` are in the module docstring
(lines 14, 16) or comments (530, 556, 1797, 2048); **0** in `install_bridge.py`. The module
has exactly **three** `atomic_write` call sites — `pending.jsonl` (:2124),
`checkpoint.json` (:2296), `cross-project-fact-fingerprints.json` (:2354) — all under
`write_candidates_root`. The only other `os.open` calls are the lock file (:501) and two
transcript reads (:657, :691), both `O_RDONLY | O_NOFOLLOW` with an `fstat` identity
re-check after open.

### The live `UserPromptSubmit` path is unaffected — CONFIRMED EMPIRICALLY
Loaded the `HEAD` (pre-round) `install_bridge.py` **alongside** the new one, ran
`plan()` → `install()` → `verify()` → `uninstall()` against byte-identical fixtures at an
**identical filesystem path**, and compared 18 output fields — including `release_id`, the
full `command` string, both `hooks.json` files verbatim, `policy.json` verbatim, receipt
keys and receipt-row keys, plan config keys, and release-dir contents.

**All 18 identical.** Base `release_id` `49d34103ed0e7582…` both sides. Additionally: base
`policy.json` has no `write_trigger` key; base release dir contains only the 2 original
files; base `hooks.json` has no `SessionEnd` key. Every new code path is behind
`if write_trigger is not None:` and a default call never reaches it.

### Live installed state is untouched — CONFIRMED
```
LIVE claude_memory_hook.py sha256 == working-tree copy   (3a02bb06ecce211f…)
LIVE release dir contents: ['claude_memory_hook.py', 'policy.json']   <- no write_candidate_capture.py
LIVE receipt keys: no write_trigger_script_sha256, no write_trigger_bridge_id
LIVE policy.json: no write_trigger block
```
The write trigger is **not installed**, as expected, and the live redaction fix is current.

### Redaction pipeline unaffected — CONFIRMED
Neither piece modifies `claude_memory_hook.py`; this round's diff touches only
`install_bridge.py`, `write_candidate_capture.py`, and the two test files.
`write_candidate_capture.py` only *consumes* `hook.redact()`. `hook.validate_policy`
(`claude_memory_hook.py:196`) was widened in a **prior** round to tolerate the optional
`write_trigger` key via `optional_keys = {"write_trigger"}`, and the live installed copy
already carries that tolerance (sha match above) — so a write-trigger `policy.json` would
not break the live read-side hook. Worth stating explicitly since `make_release()` writes
`write_trigger` into the **same** `policy.json` the live hook reads.

---

# Summary of required fixes

**Piece 2 (blocking):**
1. **P0-1** — make `default_write_candidates_root()` resolvable at hook runtime; add a
   subprocess regression test that runs the real command string from the release directory.

**Piece 1 (blocking):**
2. **P1-A** — add an anchored compaction/resume-summary marker to G1b (61 → 0 live FPs).
3. **P1-B** — degrade a corrupt/unreadable cross-project registry to *empty* rather than
   killing every project's scan; add tests for the persistence layer.

**Recommended alongside:** P2-A (prune on load), P2-B (explicit ordering key), P2-C (apply
G1b to the T3/T5 path), P2-D (`session_file` equality check in `_match_t1_t2`).

---

**Reiterating: a GO on either piece would still not mean `install-write-trigger` should be
run. That is a separate decision requiring the human's own explicit go-ahead.** Both pieces
are NO-GO as they stand.
