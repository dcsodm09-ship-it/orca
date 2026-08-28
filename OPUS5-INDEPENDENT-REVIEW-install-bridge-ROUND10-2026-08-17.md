# Independent review — `install_bridge.py` round 10 (candidate `6fb376c671`)

- **Reviewer:** Claude `opus5` / effort `max`, independent read-only pass
- **Date:** 2026-08-17
- **Candidate:** `6fb376c671` *fix(install_bridge): guard the install-rollback commit and stop treating unreadable as unowned*
- **Baseline for the increment:** `cb1c532459` (round-9 candidate)
- **Files in scope:** `claude-codex-memory-bridge/install_bridge.py`, `claude-codex-memory-bridge/tests/test_install_bridge.py` (the commit touches exactly these two — verified with `git show --stat`)
- **Interpreters:** `/usr/bin/python3` 3.9.6 and PATH `python3` 3.14.6 (Homebrew)

## Verdict

# **GO**

Both round-9 blockers are genuinely fixed. I reproduced each one on the round-9 baseline
with my own scenarios (my own account names, my own SIGKILL trigger, my own permission
values — nothing copied from the candidate suite), then confirmed the candidate refuses
in every one of them, on both interpreters. No regression in any round 1–9 fix. The
suite is really green (82/82 on both interpreters), and all three new tests are
genuinely non-vacuous against the baselines they claim to distinguish.

**No reproducible P0 or P1 was found in this candidate.** Six findings are recorded
below at P2/P3. The most significant, **R10-P2-A**, is a real availability regression
introduced by this round (an object that has nothing to do with this installer can now
wedge `install()` into a pending-journal state), but it is fail-closed, never abandons a
live handler, always names the offending path on the operator's next command, and is
cleared by an ordinary `chmod`/`mv`/`rm` on that object — so it does not meet the
blocking bar this file's previous nine rounds have used.

---

## Note on the dispatch channel

`orca orchestration send --dispatch-capability dcap_GkHJ…` returned
`Dispatch ctx_68ce719674e3 capability is revoked.` on my first heartbeat, so no
heartbeats could be emitted during this run and `worker_done` may not land either. The
review itself was unaffected; this report is the durable deliverable.

---

## (a) R9-P1-A — install-side rollback commit — **FIXED (independently reproduced both ways)**

### My scenario (deliberately not the candidate's)

Fixture: 3 pooled accounts (`pooled-alpha`, `pooled-bravo`, `pooled-charlie`) plus the
`.codex` home = 4 discovered configs, each with *distinct* pristine content so every
digest differs. Kill trigger: a real `SIGKILL` in a **child process**, fired the moment
`atomic_write()` is called with a target whose basename is `latest-receipt.json` — a
*semantic* kill point derived from the file being written, not the candidate suite's
hard-coded `kill_after_atomic_write_calls=11`. Machine is genuinely fresh: never
installed, no receipt has ever existed.

Post-kill state verified explicitly: journal present, `latest-receipt.json` absent, all
four live configs already carrying 2 handlers.

### a1 — sibling rename

`codex-accounts/pooled-bravo` → `codex-accounts/pooled-bravo.retired-2026-08`, then every
recovery path:

| path | round 9 (`cb1c532459`) | round 10 (`6fb376c671`) |
|---|---|---|
| `recover_pending_install()` | `{"ok": true, "state": "rolled_back"}` — **journal deleted, relocated handler abandoned, no receipt had ever existed** | refuses, names the relocated path |
| `uninstall()` | `not installed: no receipt found` (journal already destroyed by the above) | refuses |
| `install()` | refuses (R6-P1-A only, by luck of the one-level shape) | refuses |
| after renaming back | nothing left to recover (`state: none`), `pooled-bravo` left permanently bridged | `state: rolled_back`, **all four configs back to 1 handler**, journal cleared, still no receipt |

That is R9-P1-A reproduced end-to-end on the baseline and closed on the candidate. No
permanent lockout: restoring the directory lets the rollback finish normally.

### a2 — the hidden "deeper location" variant

Same kill, then `codex-accounts/pooled-charlie` →
`codex-accounts/_attic/2026/q3/pooled-charlie` (below the one-level shape
`_enumerate_hook_configs()` understands, so `discover_hook_configs()` cannot see it and
R6-P1-A never fires):

- round 9: `install()` **returned a receipt — it succeeded** — and `recover` reported
  `state: none`. Exactly the variant the dispatch flagged.
- round 10: `install()` and `recover` both refuse; no receipt written; restoring the path
  lets the rollback complete.

### a3 — clean install, then deep relocation, then `install` again

- round 9: `install()` succeeds silently with the deep orphan live.
- round 10: `install()` raises `install failed and the durable recovery journal remains
  pending`; `recover` / `uninstall` / a second `install` all refuse **with the useful
  message naming the path**; `verify` reports the pending journal; `plan` still answers.
  Restoring the account → `recover` returns `committed`, `verify` OK, `uninstall` OK, all
  configs pristine.

**39/39 independent assertions pass on both interpreters.**

**One thing this round did *not* close** — see **R10-P2-B**: the shared scan sits at
`install()`'s *commit*, i.e. after `write_runtime()` has already re-materialised the
release directory the orphan's handler points at.

---

## (b) R9-P1-B — `_read_for_detection()` — **FIXED (no new false positives on benign files)**

### b1 — permission-bit combos (mine, not the candidate's `0o664`)

Clean install → relocate `pooled-bravo` → `chmod` the relocated config, then `uninstall()`:

| mode | meaning | round 10 |
|---|---|---|
| `0o646` | other-writable | refuses, `does not track` |
| `0o620` | group-writable only | refuses |
| `0o604` | other-readable, no write | refuses |
| `0o777` | everything | refuses |
| `0o666` | group+other writable | refuses |

In every case `latest-receipt.json` survives the refusal. On round 9 each of these was
silently waved through by `validate_owned_file()`'s unrelated write-safety policy.

### b2 — ownership

With `os.getuid()` returning a foreign uid for the duration of the scan (the condition a
sudo-driven restore produces), `_find_untracked_owned_configs()` still returns the
relocated live config. The uid question is no longer asked at all — correct.

### b3 — genuinely undecidable inputs now fail closed

- relocated config `chmod 0o000` → `uninstall` refuses with `cannot read <path>`
- relocated config padded past `MAX_MANAGED_FILE_BYTES` (5 MiB) → refuses with
  `cannot safely inspect <path>: too large`

Both correct: an unreadable file cannot be proven handler-free.

### b4 — false-positive control (the important half)

Fourteen unrelated `hooks.json` objects planted under
`.claude/plugins/marketplaces/.../hooks/` — mirroring the six that really exist on this
machine — at modes `0o666`, `0o664`, `0o777`, `0o644`, `0o600`; plus malformed JSON,
raw binary, empty, `{"hooks": []}`, `{"hooks": null}`, `{"hooks": "nope"}`, duplicate
keys, a command merely *containing* `BRIDGE_ID` as a substring, a **directory** named
`hooks.json`, a **FIFO** named `hooks.json`, and a **dangling symlink**:

- `uninstall()` **succeeds**, all managed configs restored to pristine.
- The FIFO does not hang the scan (`lstat` rejects it before `open`).
- With `os.getuid()` forced to `501`, `1500`, and `0`, the scan flags **only** the
  genuinely relocated account in all three cases.

### b5 — read-only run against the real machine

I ran the candidate's `_find_untracked_owned_configs()` against the real
`/Volumes/Extreme SSD/Orca/local-homes` (read-only — `os.walk` + `stat` + `O_RDONLY`;
no install, no write, nothing touched):

```
LOCAL_HOMES_ROOT = /Volumes/Extreme SSD/Orca/local-homes
RUNTIME_BASE     = .../.shared-runtime/claude-codex-memory-bridge   exists=False
pending journal  exists=False        latest-receipt exists=False
scan completed cleanly; owned handlers found at untracked paths: 0
```

The bridge has still never been installed on this machine. The six unrelated plugin
`hooks.json` files (all `0o644`, uid 501, 291–4393 bytes) produce **no** false positive
and **no** refusal.

**23/23 independent assertions pass on both interpreters.**

---

## (c) The test-vacuity fix — **CONFIRMED**

`test_uninstall_ignores_an_untracked_config_with_malformed_content_at_the_one_level_shape`,
run with the candidate's test file against each historical module:

| module | result |
|---|---|
| round 8 `37d2433a77` | **ERROR** — `AttributeError: 'list' object has no attribute 'get'` at `install_bridge.py:1254`, `candidate_payload.get("hooks", {}).get("UserPromptSubmit", [])` |
| round 9 `cb1c532459` | ok |
| candidate `6fb376c671` | ok |

Bare `AttributeError` reproduced exactly as claimed. I also confirmed the *old* broad
test (`…_with_malformed_or_unexpected_content`) **passes** unchanged against round 8 —
i.e. it really was vacuous, which is what motivated the companion.

I additionally checked the other two new tests are non-vacuous against the round-9
module: both fail there with `AssertionError: InstallError not raised`.

---

## (d) Test suite — **REALLY GREEN, both interpreters**

```
$ cd claude-codex-memory-bridge
$ /usr/bin/python3 -m unittest discover -s tests -v      # Python 3.9.6
Ran 82 tests in 1.016s — OK
$ python3 -m unittest discover -s tests -v               # Python 3.14.6
Ran 82 tests in 0.842s — OK
```

Per-class counts identical on both: `InstallBridgeTests` 9, `InstallEndToEndTests` 31,
`UninstallCrashRecoveryTests` 2, `InstallCrashRecoveryTests` 1 → **43 `install_bridge`
methods** (40 → 43 as claimed), plus `ClaudeMemoryHookTests` 39 = 82 total. The
dispatch's "79/79 → 82/82" is the whole `discover` run; the `install_bridge`-only count
is 40 → 43. Both figures check out.

---

## (e) Regression spot-checks, rounds 1–9 — **NO REGRESSIONS (76/76, both interpreters)**

Written independently of the candidate suite:

| # | fix | probe | result |
|---|---|---|---|
| e1 | **P1-2** uninstall journaling | real `SIGKILL` when `atomic_write` first targets a `codex-accounts/**/hooks.json`; verified the half-uninstalled mix `[1,2,…]` really existed | recovery returns `uninstalled`, all pristine, receipt + journal cleared |
| e2 | `owned_handler()` exact match | 12 command vectors (`--bridge-id=X`, `X` as substring, `--bridge-idz`, unquoted `#` comment, **quoted** `'#'`, unbalanced quote, `"--bridge-id" "X"`, bare trailing flag) + 8 malformed handler objects | all 20 correct, no crashes |
| e3 | 5+2 `stat()` guards | `_mode_bits` on ENOENT; `_path_is_absent` / `_is_regular_file` / `resolve_ssd_path` on EACCES; ENOENT still means "absent"/"not a file" | all raise clean `InstallError`; ENOENT semantics intact |
| e4 | **R2-P1-A** `prev_*` vs `before_*` | change the source script → new `release_id` → real upgrade → `SIGKILL` before `latest-receipt.json` | rolls back byte-for-byte to the **previous working install**, still bridged, `verify` OK |
| e5 | **R2-P1-B** lock dir creation | virgin machine, `.shared-runtime` absent, via `main()` in a child | install succeeds; both levels created `0o700`; `verify` OK afterwards |
| e6 | **R3-P1-A** retired account | `rm -rf` one account | `verify` + `uninstall` both succeed and report it `unreachable` |
| e7 | **R3-P2-A / R4-P2-A** | carry a row forward, then delete the older backup directory | `uninstall` still succeeds |
| e8 | **R4-P1-A** fail-closed | `chmod 0o000` on an account's `home/` | `verify` and `uninstall` both fail closed, receipt survives, **no** pending journal; works again once readable |
| e9 | **R5-P1-A** eager backup load | `rm -rf` the whole `backups/` tree | `uninstall` refuses **before** writing the journal; nothing wedged; configs untouched |
| e10 | **R5-P2-A** early determination | carried-forward row made indeterminate (`chmod 0o000` on the account dir) | refuses with nothing written, no journal |
| e11 | **R6-P1-A** install-side | one-level rename of a bridged account | `refusing to record an already-bridged config as a pristine baseline`, receipt survives |
| e12 | **R7/R8/R9** uninstall scan | 5 relocation shapes: sibling rename, deeper under `codex-accounts`, out of `codex-accounts`, inner `home/` renamed, into a dot-directory | all five refuse with `does not track`; receipt survives each time |

One expectation of mine needed loosening, not the code: in e8 the *scan's* unlistable-
directory guard (R8-P2-B) now fires before `_receipt_rows()`, so the message is
`cannot list …: [Errno 13]` rather than `cannot determine whether … exists`. Still
fail-closed; only the wording changed.

---

## (f) New problems — six findings, none blocking

### R10-P2-A — an object unrelated to this installer can now wedge `install()` *(new this round)*

**What.** The shared scan now runs on `install()`'s commit path. `_find_untracked_owned_configs()`
escalates (does not skip) three conditions that can be produced by files this installer
neither wrote nor manages:

1. any directory under `local-homes` that exists but cannot be listed (`onerror` →
   `InstallError`, R8-P2-B's guard),
2. any `hooks.json` under `local-homes` that cannot be read (`_read_for_detection` → `cannot read`),
3. any `hooks.json` under `local-homes` larger than `MAX_MANAGED_FILE_BYTES`
   (`cannot safely inspect …: too large`).

**Reproduced** (fresh fixture, bridge installed, then a single unrelated object created):

```
mkdir -p local-homes/.cache/other-tool/private && chmod 000 .../private
install  -> refused: install failed and the durable recovery journal remains pending
pending journal left behind: True
verify   -> refused: pending install journal must be recovered first
uninstall-> refused: cannot list .../.cache/other-tool/private
plan     -> ok
```

Identical with an unrelated `hooks.json` at `chmod 000`, and with an unrelated
`hooks.json` of 5 MiB. On the round-9 baseline all three cases let `install()` **succeed**
— so the *install-side* half of this is new this round.

**Why it is not P1.** It is fail-closed; no live handler is ever abandoned; the pristine
baseline is never lost; the wedge is **not permanent** — a `chmod`/`rm`/`mv` on the
unrelated object clears it, and I verified `recover` then returns `committed` and
`uninstall` works normally. The operator is not left blind either: the offending path is
named by `recover`, by `uninstall`, and by a second `install` (only the *first* failure
message, `install failed and the durable recovery journal remains pending`, carries no
path — that is the sharpest edge here).

**Note on the new function's own comment.** It states the design goal as *"only actual
read failures escalate, not 'not owned by us' or 'not private'"*, to avoid giving an
unrelated file a permanent veto. Two conditions escape that description: the **size cap**
escalates although the comment itself lists it as one of the *wrong* questions for
detection; and an unrelated file that is *both* foreign-owned *and* `0o600` **is** an
actual read failure, so "not owned by us" does in fact escalate in the case that matters.
The behaviour is defensible — you cannot prove an unreadable file handler-free — but the
comment understates the surface.

**Suggested (out of scope for this review):** make the size cap skip-with-refusal only
for candidates that are plausibly ours, and give the operator a documented escape hatch;
also propagate the underlying `InstallError` message through `install()`'s own
`except BaseException` so the first failure names the path.

### R10-P2-B — the shared scan still runs too late inside `install()`

`install()` calls `recover_pending_install()` twice: at line 1036 (returns immediately
when no journal exists — so **no scan**) and at line 1310, *after* `write_runtime()`,
*after* every discovered config has been rewritten, and *after* `latest-receipt.json`.

Consequence, reproduced on a machine whose runtime release had been wiped while a deep
orphan still carried a live handler pointing at it:

```
release dir wiped: exists=False
install -> refused: install failed and the durable recovery journal remains pending
>>> release dir re-materialised by the refused install: True
    script back=True  policy back=True  -> the orphan's hook command is executable again
```

So the refusal does not prevent the orphan's hook from becoming executable again — it
only prevents `install()` from *reporting success*. Round 9 behaved identically **and**
reported success, so this is a net improvement, not a regression; but the round's own
framing ("neither branch can report success while abandoning a live handler") does not
extend to "nothing is resurrected before the refusal".

This is also what turns R10-P2-A into a *wedge* rather than a clean refusal. Running the
same scan at the **start** of `install()`, before `write_runtime()`, is the discipline
R5-P1-A and R5-P2-A already established for this exact class ("refuse before anything
becomes durable") and would collapse both P2s at once.

### R10-P2-C — a live handler can still hide from the scan behind ordinary encoding quirks *(new blind spot, not in the round-9 report)*

`_contains_owned_handler()` → `strict_json()` demands strict UTF-8, no duplicate keys,
and no trailing content. Measured with a config that genuinely contains the exact
generated `--bridge-id <BRIDGE_ID>` argv pair:

| content shape | `_contains_owned_handler` |
|---|---|
| canonical (control) | **True** |
| extra unrelated top-level key | True |
| handler plus an extra event key | True |
| **UTF-8 BOM prefix** | **False** |
| **duplicate top-level `"hooks"`, ours last** | **False** |
| **trailing `// comment` line** | **False** |
| **UTF-16 encoded** | **False** |

Each `False` row is a file whose handler a permissive consumer would still execute, at a
path `uninstall`/`recover` would then report `ok:true` for while deleting the receipt.
Reachability is low — everything *this tool* writes goes through `canonical_json()`, so
only third-party rewriting or hand-editing of a relocated config produces these — which is
why I rate it P2 rather than P1, unlike R9-P1-B whose trigger (`chmod 0664` from a
`cp`/`rsync`/archive restore) is a routine side effect.

### R10-P3-A — `_read_for_detection()` dropped `validate_owned_file()`'s post-read identity check

`validate_owned_file()` ends with `after = os.fstat(descriptor)` plus
`before_identity != after_identity or len(raw) != after.st_size` → `file changed while
reading`. `_read_for_detection()` has **neither**. A config rewritten in place (same
inode, so the `dev/ino` check cannot see it) during the scan's read yields a short/torn
buffer, `strict_json()` fails, and the file is classified "not one of ours".

Reproduced by rewriting the victim between `os.fstat()` and the read loop: the scan
returned **without** the victim, and the control run (file intact) returned it. The same
input was also missed on round 9 (whose `except InstallError: continue` swallowed the
`file changed while reading` error), so this is not a regression — but it is the *exact
shape* of R9-P1-B ("cannot reliably determine" silently rendered as "not ours") surviving
inside the very function written to eliminate it. Requires a concurrent in-place writer,
hence P3.

### R10-P3-B — the resolve step above the fixed line still fails open

```python
try:
    resolved = resolve_ssd_path(candidate)
except InstallError:
    continue                      # <-- still swallows "cannot determine"
candidate_raw = _read_for_detection(resolved)   # <-- this round's fix
```

Round 10 corrected the *read* step and left the *resolve* step one line above with the
old fail-open shape. `resolve_ssd_path()` raises for: a symlink resolving off the SSD
(the known R9-P3-B), a resolved path on a **different `st_dev`** (a nested mount or disk
image under `local-homes` — *not* mentioned anywhere in the round-9 report), and any
`OSError` from `resolve(strict=True)` / `stat()`.

Confirmed empirically for the symlink instance: with a live owned config off-volume and
`codex-accounts/…/home/hooks.json` a symlink to it, `uninstall()` returned
`{"ok": true, …}` and deleted the receipt. Round 9 behaves identically. P3 because both
shapes require a hand-made symlink or a hand-made nested mount — `discover_hook_configs()`
would never have installed into either.

### R10-P3-C — symlinking a managed account directory is a false positive *(unreported, pre-existing, now also blocks `install`)*

`receipt_paths` is built from the **raw** receipt strings, while the scan compares
**fully resolved** paths. Replace `codex-accounts/pooled-bravo` with a symlink to
`_store/pooled-bravo` (both under `local-homes`) — the managed config is still perfectly
reachable at its receipt-recorded path, `verify()` returns `ok:true` — yet:

```
uninstall -> refused: found an owned hook handler at a path the current receipt
             does not track (… restore it to its receipt-recorded path …)
```

The remediation the message gives is unactionable here: the file *is* at its
receipt-recorded path. Identical on round 9, so pre-existing since round 8's whole-tree
walk; this round extends the same false positive to `install()`.

### R10-P3-D — scan cost is roughly 4–30× the figure carried in the round-9 report

`os.walk` alone over the real tree: 0.56 s (5712 directories). The full
`_find_untracked_owned_configs()` — which also `stat()`s a `hooks.json` candidate in
every directory — measured **7.91 s cold**, then 3.69 s / 2.68 s / 0.87 s on repeats.
`uninstall()` runs it **twice** (its own call plus the commit `recover`). Not a
correctness issue; worth recording because 0.24 s is a warm best case, not the number an
operator will see on a cold external SSD.

---

## Answers to the three questions the dispatch asked under (f)

**① Did moving the scan break the `kind == "install"` branch's own logic?**
No. I re-verified each downstream decision with the scan now upstream of it:

- committed install whose carried-forward row is genuinely `absent` → still `committed`
  (`SIGKILL` fired inside `remove_file_durable` just before the journal was cleared);
- drift during a pending install → still `pending install cannot roll back because a config drifted`;
- drift during uninstall → still `refusing uninstall because a config changed: <path>`;
- `latest_matches_this_receipt` still discriminates correctly in both directions.

The only ordering *consequence* is message priority (the scan's error now shadows the
drift error when both apply) and the two P2s above, which are about *placement inside
`install()`*, not about this branch's internal logic.

**② TOCTOU across `_is_regular_file` → `resolve_ssd_path` → `_read_for_detection`?**
No crash and no hang in any variant. I swapped the candidate between the first check and
the read with: deletion, a directory, a **FIFO**, a symlink to `/dev/null`, and a
different regular file. All five degraded to a clean skip or a clean `InstallError`;
`uninstall()` never produced a bare traceback and never blocked on the FIFO (the `lstat`
`S_ISREG` test runs before `open`, and `O_NOFOLLOW` + the `dev/ino` comparison catch the
rename-style swap). The one residual window is R10-P3-A (same-inode in-place rewrite),
which no `dev/ino` check can see and which only a post-read re-`fstat` would catch.

**③ Is this round still "treating the symptom", and are there blind spots beyond
R9-P2-A / R9-P3-A / R9-P3-B?**
Yes on both counts, and I would state it more sharply than the round-9 report did. The
round-10 fix makes the *commit* side airtight — I confirmed every receipt/journal-deleting
commit in the file is now behind the shared scan — but the scan is still a **search**, and
I found three further blind spots by attacking the search rather than the commit sites:

- **content encoding** (R10-P2-C): BOM, UTF-16, duplicate keys, trailing text — a live
  handler that a permissive parser executes but `strict_json()` refuses to see;
- **read-window** (R10-P3-A): the one `validate_owned_file()` check the new function
  dropped;
- **resolve-window** (R10-P3-B): a different-`st_dev` nested mount under `local-homes`,
  which nothing in the round-9 report covers.

Plus one *false* positive of the same origin (R10-P3-C). None of these is closable by
another local patch to the scan, which is precisely the round-9 report's own "item 3"
argument: as long as "no evidence found by searching" is what authorises deleting a
receipt, each round buys one more shape. The structural alternative — requiring a
receipt row to be *provably* `absent`-and-previously-`after`, plus a recorded operator
retirement acknowledgement, before any receipt-deleting commit — would subsume
R9-P2-A, R9-P3-B, R10-P2-C, R10-P3-A, R10-P3-B and R10-P3-C at once. Deferring it this
round was the right call (it is a design change, not a patch), but I would not defer it
again: the scan is now load-bearing on **four** commit paths instead of one, and R10-P2-A
shows the cost of that load is starting to fall on operations that have nothing to do
with the hazard.

---

## Reproduction assets

All scenarios ran in isolated `tempfile.mkdtemp()` fixtures with the module constants
patched (`SSD_ROOT`, `LOCAL_HOMES_ROOT`, `RUNTIME_BASE`, `PENDING_PATH`, `SOURCE_SCRIPT`,
`volume_uuid`, `Path.home`). Nothing was installed, activated, written, or committed on
the real `/Volumes/Extreme SSD`; the only real-machine access was read-only (`os.walk`,
`stat`, `O_RDONLY`), and the real bridge remains uninstalled (`RUNTIME_BASE` does not
exist).

Harness (scratchpad, not committed):
`envkit.py` (fixture + real-SIGKILL child runner), `scen_a.py` (39 assertions),
`scen_b.py` (23), `scen_e.py` (76), `scen_f.py` / `scen_g.py` (adversarial probes),
plus `base_r8/` and `base_r9/` module snapshots extracted with `git show` for the
non-vacuity and delta runs.

## Summary table

| ID | severity | new this round? | status |
|---|---|---|---|
| R9-P1-A install-rollback commit unguarded | P1 | — | **fixed & independently verified** |
| R9-P1-B unreadable/odd-mode treated as unowned | P1 | — | **fixed & independently verified** |
| R9-P3-C vacuous malformed-content test | P3 | — | **fixed & independently verified** |
| R10-P2-A unrelated object wedges `install()` | P2 | **yes** | open |
| R10-P2-B scan runs after `write_runtime()` in `install()` | P2 | partly | open |
| R10-P2-C encoding-shaped detection blind spots | P2 | no (unreported) | open |
| R10-P3-A no post-read identity check in `_read_for_detection` | P3 | no (unreported) | open |
| R10-P3-B resolve step still fails open (incl. nested mounts) | P3 | no (partly unreported) | open |
| R10-P3-C symlinked managed account = false positive | P3 | no (unreported) | open |
| R10-P3-D scan cost 4–30× the reported figure | P3 | no | open |
