# OPUS5 Independent Adversarial Review — `write_candidate_capture.py` ROUND 2

- **Date:** 2026-08-19
- **Reviewer:** Claude opus5 / max effort, independent read-only session (no memory of round 1)
- **Candidate:** `完善orca/claude-codex-memory-bridge/write_candidate_capture.py` (new, additive, not wired in) + a 9-line change to `claude_memory_hook.py`
- **Parallel review:** an independent Codex review ran concurrently on the same candidate; no coordination.
- **Verdict:** **NO-GO** — 1 new P1 (reproduced against real files on this machine), 4 P2. 4 of the 5 round-1 P1s are genuinely closed; P1-3 is only partly closed.

---

## Test suite (re-run by this reviewer)

```
/usr/bin/python3 -m unittest discover -s tests -v
Ran 172 tests in 10.066s — OK
  test_claude_memory_hook       39 OK
  test_install_bridge           59 OK
  test_write_candidate_capture  74 OK
```

172 passing as claimed (161 → 172, +11).

## Change scope

Inside `claude-codex-memory-bridge/`, exactly as claimed:

```
 M claude-codex-memory-bridge/claude_memory_hook.py        (+9 −1, one hunk)
?? claude-codex-memory-bridge/AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md
?? claude-codex-memory-bridge/tests/test_write_candidate_capture.py
?? claude-codex-memory-bridge/write_candidate_capture.py
```

**Deviation from the brief's step 5:** `prime-agent-integration/install_prime_agent.py` and
`prime-agent-integration/tests/test_install_prime_agent.py` are also modified. Assessed as an
unrelated concurrent workstream, not contamination from this fix pass: `git log` shows active
prime-agent round-28/29/30 work, file mtimes (22:05, 21:45) bracket the write module's (21:59),
and `grep` finds zero cross-references to `memory_hook` / `memory-bridge` / `write_candidate`
in either file. Flagged for the human to confirm, not treated as a finding against this candidate.

---

## P0

None.

---

## P1-A (NEW — residual of round-1 P1-3): a session file containing any single JSONL record larger than the per-invocation byte budget is permanently stalled at that record. Every line after it is never scanned — silently, forever.

**Location:** `write_candidate_capture.py:650-702` (line-boundary loop), `:510-559`
(`_read_new_session_bytes`), `:92` (`MAX_SCAN_BYTES_PER_INVOCATION = 4 MiB`)

**Mechanism.** `read_length` is capped at the remaining budget (`:542`). If the returned block
contains no `\n` — i.e. the next record on its own is bigger than the budget — then
`raw.find(b"\n", position)` returns `-1` and the loop `break`s immediately (`:651-652`).
`file_byte_offset` was initialised to `start_byte` (`:648`) and is never updated, so
`new_file_progress[path.name] = {"byte_offset": start_byte, ...}` (`:698`) records **zero
advance**. The next scan repeats byte-for-byte. There is no max-line handling and no path that
skips past an over-budget record.

**Reproduced (probe D).** File with one 4.25 MB record followed by a genuine T1 correction:

```
scan 1: turns=['ordinary small session turn']  big-file progress={'byte_offset': 0, 'line_index': -1}
scan 2: turns=[]                               big-file progress={'byte_offset': 0, 'line_index': -1}
...
scan 6: turns=[]                               big-file progress={'byte_offset': 0, 'line_index': -1}
```

The correction sitting after the giant record is never extracted, at any scan count.

**Real files on this machine right now** (65 of 308 transcripts exceed 4 MB; 2 contain a single
record over the budget):

| file | size | stalls at | that record | permanently unreachable |
|---|---|---|---|---|
| `~/.claude/projects/-Volumes-Extreme-SSD-Orca-workspaces-ai-----------/68a754c8-4dd7-43b3-a598-63d3dcc66694.jsonl` | 87,348,493 B | line 946 | 5,400,717 B | 84,174,814 B — **96.4%** |
| `~/.claude/projects/-Volumes-Extreme-SSD-Orca-workspaces-hgfast-nat-oci-.../b5020693-d635-4862-87ac-61cb3bedbcd7.jsonl` | 34,496,703 B | line 5900 | 4,840,488 B | 16,763,428 B — **48.6%** |

The stalled file also re-reads up to 4 MiB on *every* `SessionEnd` forever with zero progress —
a permanent I/O tax, not just lost data.

**Documented invariants that are false.** Module docstring `:592-596`: *"The oversized file
itself still gets whatever budget remains, read incrementally (bounded to a complete prefix)
rather than skipped outright, so multi-invocation progress on it is still made."* And design doc
§6 P1-3(a): *"a large file is drained incrementally across multiple invocations rather than
either blocking everything or being permanently abandoned."* Both are false for the two files above.

**The shipped regression test uses the exact triggering input and does not catch it.**
`tests/test_write_candidate_capture.py:687` (`test_oversized_file_does_not_block_other_files_in_project`)
builds a file whose single record is `MAX_SCAN_BYTES_PER_INVOCATION + 64 KiB` — precisely the
pathological case — then asserts only that (a) it appears in `oversized`, (b) the *other* file's
turn is read, (c) the *other* file is in `file_progress`. It never asserts the oversized file
advances. This is exactly round 1's own warning: green tests, adjacent property, uncovered bug.

**The added mitigation has no consumer.** `ScanResult.oversized_files_skipped` (`:993`) is the
fix's answer to "don't read 0 candidates as nothing happened," but `_main_scan` (`:1505-1511`)
calls `scan(...)` and discards the return value entirely. In the only production entry point the
distinction is unobservable.

**Why P1, not P0:** cannot corrupt `MEMORY.md`, leak secrets, or break the live read hook — it
only silently under-proposes. **Why not P2:** permanent, unrecoverable, silent loss of the
majority of two real transcripts, in the exact area round 1 flagged, with a false documented
invariant and a regression test that looks like coverage but is not.

**Fix direction:** when `capped and newline_at == -1`, either allow a single record to be read up
to `MAX_SCAN_BYTES_PER_FILE`, or locate the next `\n` with a bounded seek and advance
`byte_offset` past the oversized record (counting it as skipped), so terminal progress is
guaranteed. Add an assertion that the oversized file's `byte_offset` strictly increases across
scans until drained.

---

## P2-B: `load_checkpoint` does not validate `working_set` entry shape → uncaught `AttributeError` escapes both `scan()` and `_main_scan`, breaking the "always exit 0" contract

**Location:** `write_candidate_capture.py:1321-1323` vs `:1324-1338`

`file_progress` **is** validated per entry (name matches `_SESSION_FILE_RE`, both fields `int`).
`working_set` gets only a top-level `isinstance(dict)` check — its entries are trusted verbatim.
Consumers assume shape: `:1116` `entry.setdefault("sessions", [])`, `:1118` `sessions.append(...)`,
`:1124` `entry.get("sample", text)[:800]`, `:1134` `entry.get("promoted", False)`.

**Reproduced (probe A):**

```
[entry['sessions'] is a string] -> UNCAUGHT AttributeError: 'str' object has no attribute 'append'
[entry itself is a string]      -> UNCAUGHT AttributeError: 'str' object has no attribute 'setdefault'
      caught by _main_scan's (WriteCaptureError, OSError, ValueError)? False
```

`_main_scan:1512` catches `(WriteCaptureError, OSError, ValueError)` — `AttributeError` is none of
these, so the hook exits non-zero with a traceback. Identical contract violation to round-1 P1-1,
via a different source.

**Why P2 not P1:** requires non-conforming `checkpoint.json` content. `atomic_write` prevents torn
writes and the file is created 0600, so realistic routes are a human hand-editing it (plausible —
this directory is explicitly a human-review surface), disk corruption, or a future promotion tool.
Round-1 P1-1's trigger (ordinary disk-full) was strictly more likely.

This is the direct answer to the brief's "are there still reused-primitive exceptions not in the
caught set": not from the reused primitives — a fault-injection sweep confirmed `InstallError`
from the real `install_bridge.atomic_write` no longer escapes — but yes from the module's own
unvalidated persisted state.

---

## P2-C: `load_checkpoint` does not enforce the 0600 privacy bits it enforces for `pending.jsonl`

**Location:** `write_candidate_capture.py:1302-1305` vs `:1178-1179`

`load_checkpoint` checks `S_ISREG`, not-symlink, `st_uid`, and a size bound — but has no
`info.st_mode & 0o077` check. `_read_existing_pending` has exactly that check for `pending.jsonl`.

**Reproduced (probe B):**

```
created modes: checkpoint.json 0o600   pending.jsonl 0o600
load_checkpoint on a 0666 checkpoint.json      -> ACCEPTED (no privacy check)
_read_existing_pending on a 0666 pending.jsonl -> refused: "pending.jsonl is not private"
```

Matters more after P1-4 than before: `checkpoint.json` now holds up to 500 × 500 chars of user
prose in `working_set[*].sample`. A group/world-**writable** checkpoint is also an injection path
into human-facing candidate content — promotion at `:1124` copies `entry["sample"]` straight into
the `RawMatch` that becomes the proposal. Combined with P2-B the same file is both unvalidated in
shape and unchecked in permissions.

Direct answer to the brief's "0600/0700 still enforced": enforced on every write and on the
`pending.jsonl` read path; **not** on the `checkpoint.json` read path.

---

## P2-D: a session file that shrinks is permanently and silently excluded, with no recovery and no reporting

**Location:** `write_candidate_capture.py:534-539`, caller `:637-641`

`new_length < 0` → `return None, False` → caller carries `prior` forward unchanged and, because
`capped` is `False`, does not even add it to `oversized_files_skipped`.

**Reproduced:** after truncating a fully-scanned file to 1/3 its size, three rescans read 0 turns
with the offset frozen at 1896; genuinely new content appended afterwards (file size 953 < 1896)
is still never read.

The comment calls this "fail closed rather than guess," but the effect is permanent, unreported
exclusion — the "silently indistinguishable from nothing happened" shape of round-1 P1-2/P1-3.
Fix: when `st_size < start_byte`, reset that file to `{byte_offset: 0, line_index: -1}` (re-derived
content is already dedup-protected) instead of freezing it.

---

## P2-E: exact-duplicate dedup silently fails for any candidate whose redacted content exceeds `max_candidate_bytes`

**Location:** `write_candidate_capture.py:1266-1268` vs `:1255` / `:1213`

`exact_hash` is computed over the **untruncated** `hook.redact(match.content).strip()`, but
`existing_hashes` is built from stored `content` values, which `make_candidate_record` already ran
through `_truncate_utf8(..., config.max_candidate_bytes)`. For over-limit content the two can
never match.

**Reproduced (probe C):**

```
redacted content bytes = 3,223 (cap 2000)
append#1 written=1 flagged=0
append#2 (identical) written=1 flagged=1
append#3 (identical) written=1 flagged=1
pending.jsonl now holds 3 records
CONTROL short content, 2nd identical append: written=0 flagged=0 (records=1)
```

Reachable in normal operation: T1 content (`:863`) embeds the **full, unbounded** user turn — only
the assistant half is `[:800]`-capped — so a long correction easily exceeds the 2,000-byte default.
Impact bounded (near-dup path still flags them; the 200 cap still holds), but it weakens
"dedup-before-write" and consumes the cap with near-identical records.

---

## P3 / observations

- `CHECKPOINT_SCHEMA` (`:62`) was **not** bumped despite a breaking field change
  (`last_scanned_session_file` → `file_progress`). Harmless today (never deployed; an old-shape
  file degrades to "nothing scanned") but it removes the only signal distinguishing the two shapes.
- `_read_new_session_bytes`'s `read_length <= 0` branch (`:544-548`) is dead code — the caller
  already `continue`s on `remaining_budget <= 0` (`:626`), so `budget` is always > 0.
- A brand-new file starved by an exhausted budget is not counted in `oversized_files_skipped`
  (`:626-630` — the `.add()` sits inside `if prior:`), understating the count.
- `scan()`'s `session_id` is validated (`:1399`) and then never used.
- `scan()`'s docstring says "Returns None … on any failure," but bare `OSError` from
  `hook.verify_storage`'s unguarded `.stat()` calls (`claude_memory_hook.py:268`, `:273`) and from
  `_ensure_private_dir`'s `lstat()`-after-`exists()` (`:307`) escape `scan()`. The **CLI** contract
  still holds — `_main_scan` catches `OSError` → exit 0.
- `_lock_path` = `write_candidates_root.parent / "installer.lock"` (`:394`): a programmatic caller
  passing `write_candidates_root == ssd_root` would have `_ensure_private_dir_chain` operate one
  level *above* the verified root. Not reachable from the CLI.
- **Operational expectation:** first-scan backlog is 1,499 MB across 308 files; at 4 MiB per
  `SessionEnd` the largest project needs ~74 scans to reach steady state. Not a defect, but worth
  stating before anyone enables this.
- `install_bridge.make_release()` (`:1059-1076`) still builds `policy.json` without a
  `write_trigger` block, so there is currently no installer-supported way to enable the feature.
  Consistent with "not wired in," but it means the P1-5 fix is forward-compatibility prep that
  cannot be exercised end-to-end until the separately-reviewed install change lands.

---

## Status of the 5 round-1 P1s

| # | Claim | Verdict |
|---|---|---|
| P1-1 | `InstallError`/`OSError`/`ImportError` escaped fail-closed boundaries | **CLOSED** (residual P2-B, different source) |
| P1-2 | Lexicographic single cursor skipped new session files | **CLOSED** (residual P2-D) |
| P1-3 | Oversized file `break`s the whole project scan | **PARTLY CLOSED — see P1-A** |
| P1-4 | Unredacted secret in `checkpoint.json` `working_set` | **CLOSED** |
| P1-5 | `write_trigger` key broke live `validate_policy()` | **CLOSED** |

**P1-1.** `atomic_write` wrapper (`:341-364`) catches `installer.InstallError` → `WriteCaptureError`;
both call sites (`append_candidates:1292`, `save_checkpoint:1357`) route through it. Both
`read_bytes()` sites now catch `OSError` (`:1184-1189`, `:1306-1312`).
`default_write_candidates_root()` moved inside the try (`:1391`) with `ImportError` in the caught
tuple (`:1410`). A fault-injection sweep over 12 reused primitives × 8 exception types confirmed
`InstallError` from the real `install_bridge.atomic_write` no longer escapes `scan()`; every other
escaping row was an artificial injection into a pure function that cannot raise that type
naturally. Tests `:911-952` genuinely reproduce the original modes.

**P1-2.** `Checkpoint.file_progress` per-file map; every present file is considered each scan
regardless of sort order. Test `:665` genuinely reproduces the original failure (the
early-sorting new file *is* read). **Unboundedness question answered:** bounded by live file count
and self-pruning — probe F confirmed 30 → 10 entries after deleting 20 files, ~86 bytes/entry; the
largest real project has 52 files ≈ 4.5 KB against a 4 MB bound. Not a growth risk.

**P1-3.** Sibling starvation genuinely fixed (ascending-unread ordering, no more `break`) — that
half is real and the test proves it. The oversized file itself never drains: **P1-A**.

**P1-4.** `hook.redact()` applied at `:1110` before storage; `sample` is the only free-text field
in `checkpoint.json`. Test `:963` asserts the raw `ghp_` token is absent and `[REDACTED_TOKEN]`
present in the on-disk bytes. Genuinely closed.

**P1-5.** `validate_policy:196-198` — `expected_keys <= set(policy) <= expected_keys | {"write_trigger"}`.
Both directions verified explicitly: base v1 policy validates unchanged; `write_trigger` accepted;
a genuinely unknown key still rejected; a missing required key still rejected (tests `:1019-1044`).
Diff is exactly +9/−1 in that file and nothing else. `install_bridge` has no independent policy
key-set check. Design doc §2.2 correctly states that enabling requires re-pinning the policy hash,
so the digest pin is not a second hidden break. Genuinely closed.

---

## Fresh general pass — invariants re-verified

| Invariant | Result |
|---|---|
| `MEMORY.md` never opened/appended/modified | **HOLDS** — probe E: byte-identical content, same `mtime_ns`, same mode across an enabled scan that wrote a candidate |
| `write_trigger.enabled` defaults `false` | **HOLDS** — `:170-175`; `scan()` returns `None` at `:1395-1396` before touching storage; candidates root not even created |
| Base v1 policy still validates unchanged | **HOLDS** — verified both directions |
| Fail-closed everywhere | **MOSTLY** — holds for all natural exception paths; gaps = P2-B, plus the `scan()` docstring/`OSError` note |
| 0600 / 0700 enforced | **MOSTLY** — dirs 0700 (`:305-320`, rejects any `0o077` bit, symlinks, foreign uid), files 0600 via `atomic_write`; gap = P2-C |
| Dedup-before-write | **MOSTLY** — exact + near-dup work; gap = P2-E for over-limit content |
| No auto-promotion path | **HOLDS** — no public promote/apply/commit entry point; every staged status ∈ {`pending`, `pending_possible_duplicate`}; `_record_and_maybe_promote` promotes working-set → `pending.jsonl` only, never to `MEMORY.md` |

---

## Verdict: **NO-GO**

Not ready to hand to a human as a reviewed, deployable-pending-authorization candidate.

One P1 with reproduced permanent silent data loss against real files present on this machine
(96.4% and 48.6% of two transcripts), whose shipped regression test uses the exact triggering
input yet asserts only an adjacent property, and whose design-doc and docstring claims are
demonstrably false. Plus four P2s, two of which (B, C) are asymmetries where the same file gets
strictly weaker validation than its sibling in the same module.

Mitigating: the fix is small and local (bounded-seek past an over-budget record, plus a strict
`byte_offset` monotonicity assertion), the module remains genuinely not wired in, and the
safety-critical invariants — `MEMORY.md` untouched, default-disabled, no auto-promotion, redaction
before persistence — all hold under direct probing. Round 3 should be short.
