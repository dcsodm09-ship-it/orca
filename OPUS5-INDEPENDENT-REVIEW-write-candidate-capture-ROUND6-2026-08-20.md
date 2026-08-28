# Opus 5 — Independent Adversarial Review, Round 6

**Target:** `完善orca/claude-codex-memory-bridge/write_candidate_capture.py` (2242 lines)
**Date:** 2026-08-20
**Interpreter:** `/usr/bin/python3` = **3.9.6** (deployment-pinned; matches the live installed hook command)
**Scope:** targeted re-review of round 6's fix pass; read-only. Installation out of scope.

---

## Verdict

**GO** — hand to a human as a reviewed, deployable-pending-authorization candidate.
**Zero P0 / zero P1.** Two P2s, two P3s.

Caveat that belongs in the handoff: **P2-1 is a regression this very round introduced**, of the same
permanent-freeze class round 6 was fixing, and round 6's own new tests structurally cannot see it.
Round 6 should not be treated as the final state — a ~5-line round 7 closes both P2s together.

---

## Verification of the two required claims

Both verified against the file, not the summary.

| Claim | Location | Status |
|---|---|---|
| `_sanitize_lone_surrogates_deep()` recursive walk exists | **1470–1501** | Confirmed. Handles `str` (1493), `dict` keys+values (1495–1498), `list` (1499–1500), all other values passed through unchanged (1501). |
| Replaces (not layers onto) field-scoped calls | **1657–1675** | Confirmed — the old `content`/`supersedes_hint` call in `_read_existing_pending` is gone, replaced by a comment explaining the move. |
| Applied in `append_candidates` | **1743** | Confirmed, before the dedup-hash computation at 1746. |
| Applied in `save_checkpoint` | **1952**, before `json.dumps` at 1953 | Confirmed. |
| `_main_list_pending` catches `UnicodeEncodeError`, print inside try | **2190 / 2191** | Confirmed. |

### Round 5's P2 is genuinely closed

Reproduced round 5's exact freeze (`working_set['trigger']`) plus three shapes round 5's
field-scoped fix would also have missed, using the realistic on-disk vector (a literal `\udXXX`
escape, which is valid UTF-8 on disk — raw surrogate bytes are rejected earlier at parse):

| Poison | `byte_offset` across 3 scans |
|---|---|
| `working_set['trigger']` (round 5's repro) | 599 → 900 → 1201 |
| `working_set` **dict key** | 599 → 900 → 1201 |
| `file_progress` **dict key** | 599 → 900 → 1201 |
| nested `working_set['sessions'][0]` | 599 → 900 → 1201 |

All progress normally. **Closed.**

### `_main_list_pending` is genuinely closed

Real CLI subprocess, lone surrogate in a **non-`content`** field (`candidate_id`):

```
exit code       : 1
stdout          : {"ok": false, "error": "'utf-8' codec can't encode character '\ud800' ..."}
stderr traceback: False
```

**Closed.**

### Non-string JSON values

`int`, `float`, `bool`, `None`, and a 2**70 big int all pass through byte-identical
(`out == sample` is `True`, types preserved). No choking, no coercion. **Correct.**

### Tests are not vacuous

207 tests, `OK`, on 3.9.6. Mutation test (neutered `_sanitize_lone_surrogates_deep` to
`return value`): **5 tests fail** (4 errors + 1 failure), all in the two lone-surrogate
test classes. The new tests genuinely bind the fix. File restored and re-verified clean
(0 mutation markers, 207 `OK`, `git status` unchanged).

---

## P2-1 — NEW REGRESSION introduced by round 6: recursive sanitizer overflows inside the decoder's accepted depth range

**`write_candidate_capture.py:1470–1501`, triggered at `:1743`, harm at `:2068`→`:2071`**

`_sanitize_lone_surrogates_deep` burns **2 Python stack frames per nesting level** — the function
frame plus the dict/list **comprehension** frame (1497, 1500), since comprehensions compile to
nested functions. Measured on 3.9.6 (`sys.getrecursionlimit()` = 1000):

```
max json.loads    depth: 993   (list and dict alike)
max sanitize-deep depth: 497
  -> WINDOW: depth 498..993 parses cleanly, then the sanitizer raises RecursionError
```

`_parse_json_fail_closed` (1552–1557) is the round-4 guard that fail-closes on over-deep input —
but it only fires above 993. Between 498 and 993 the entry parses fine, `_read_existing_pending`
keeps it (1656, `isinstance(record, dict)`), and `_sanitize_lone_surrogates_deep` at **1743** then
raises `RecursionError`.

`RecursionError` is caught by `scan()`'s catch-all at **2079** — but `append_candidates` is at
**2068** and `save_checkpoint` is at **2071**, *after* it. The checkpoint is never written, so the
poisoned `pending.jsonl` is re-read and re-crashed on every subsequent invocation.

**Reproduced.** `pending.jsonl` = one dict entry with a depth-600 value
(`{"content": "harmless", "junk": [[[...600...]]]}`), a fresh turn added before each scan:

```
scan #1..#5: result=None (FAILED CLOSED)   checkpoint byte_offset=None
```

Five consecutive scans; the checkpoint file is never even created. **A/B revert proving causality:**

```
round-6 code as shipped         : byte_offset = [None, None, None]
deep sanitizer reverted to noop : byte_offset = [301, 602, 903]
```

This is the identical permanent-freeze harm shape the module's own comments call out as the reason
round 2's P1-3, round 4's P1-2 and round 5's P2-3 mattered (see the docstring at 1457–1461) —
**reintroduced by the fix for it**, through a different trigger.

**Why round 6's tests can't see it.** Every deep-nesting fixture in the suite is
`"[" * 3000 + "]" * 3000` (`tests/test_write_candidate_capture.py:1243, 1277, 1631–1632, 2030`).
Depth 3000 is *above* the 993 decoder limit, so all of them fail-close at **parse** and structurally
never reach the sanitizer. Two independent reasons they miss it:

1. **Depth.** 3000 ≫ 993 — the 498..993 window is untested.
2. **Shape.** `_nested()` is a top-level **list**; `_read_existing_pending` drops non-dicts at 1656,
   so even at a reachable depth that fixture never enters `append_candidates`. (I hit this myself on
   my first repro attempt — it passed spuriously until I wrapped the nesting in a dict.)

`test_deeply_nested_pending_jsonl_does_not_escape_scan_or_list_pending` (tests:1698) asserts only
`assertIsNone(result)` (tests:1720) — satisfied *both* by fail-closed-at-parse and by
caught-RecursionError-then-freeze. It cannot distinguish them. Round 6's "known limitation" note on
this test is about the `assertRaises(RecursionError)` sanity check; the actual gap is the **depth and
shape of the fixture**, which the note does not cover.

**Severity calibration (deliberately not inflated):** the harm is permanent freeze, which earlier
rounds treated as P1 — but the trigger requires an externally corrupted or hand-edited
`pending.jsonl` at a specific depth band. Nothing in this module ever writes a structure deeper than
~3. That is strictly narrower than round 4's P1-2 (lone surrogates occur naturally in real Claude
Code transcripts). Round 5's opus rated the same harm shape with a hand-edit trigger P2; matching
that. Availability-only: exit 0 preserved, no bad writes, `MEMORY.md` untouched.

**Fix:** make the walk iterative (explicit stack), or bound its depth and degrade gracefully.
Note that simply lowering the cap in `_parse_json_fail_closed` does **not** work — raising
`WriteCaptureError` from `_read_existing_pending` freezes the checkpoint too (that is P2-2).

**Not affected:** the `save_checkpoint` path (1952). `load_checkpoint`'s per-entry shape validation
rejects a deep value before it can reach the payload — verified: a depth-600 value planted in
`working_set[*].sessions` still yields 599 → 900 → 1201.

---

## P2-2 — PRE-EXISTING (missed by rounds 1–5): any single unparseable line in `pending.jsonl` permanently freezes the scan

**`write_candidate_capture.py:1653` → `:2063` / `:2068` → catch at `:2079`, skipping `save_checkpoint` at `:2071`**

`_read_existing_pending` routes every line through `_parse_json_fail_closed` (1653) and lets the
resulting `WriteCaptureError` propagate. `scan()` calls it at **2063** (and again inside
`append_candidates` at 1734), catches at **2079**, and returns `None` — never reaching
`save_checkpoint` at **2071**. The file is re-read every invocation, so the failure is permanent.

**Reproduced**, fresh turn added before each scan:

| `pending.jsonl` contents | `byte_offset` across 3 scans |
|---|---|
| healthy (control) | **301 → 602 → 903** |
| one truncated/garbage line `{not json at all` | **None → None → None** |
| one line of invalid UTF-8 (`\xff\xfe garbage`) | **None → None → None** |
| depth-3000 dict entry | **None → None → None** |

Confirmed pre-existing, not round 6's doing: identical `[None, None, None]` with the deep sanitizer
reverted to a no-op.

**Why this is a defect and not intentional design.** The "only advance the checkpoint after
`append_candidates` succeeds" coupling is deliberate and correct for *transient* failures — retry
next time. It is wrong for *permanent* ones: retrying a file that will never parse never helps. The
module already knows this distinction and already implements the right pattern in two places:

- `_read_existing_pending` **already silently skips** records that survive parsing but aren't dicts
  (1656) — so a wrong-typed record is tolerated while a syntactically-invalid one is fatal.
- the transcript reader already carries an `unparseable_records_skipped` counter for exactly this
  "permanently unparseable record — skip it, count it, make forward progress" case.

Applying that same skip-and-count treatment to `pending.jsonl` lines closes **both P2-1 and P2-2**
in one change.

**Plausibility.** `atomic_write` (tempfile+fsync+replace) makes a torn write unlikely, so this needs
external corruption or a hand-edit. But the module's entire purpose is staging candidates *for human
review*, and `pending.jsonl` is the human-facing artifact — a reviewer or a future review tool
deleting a rejected candidate and leaving one malformed line silently disables the scanner forever,
with exit 0 and no diagnostic anywhere.

---

## P3-1 — Sanitizing dict keys can silently collapse two distinct keys

**`write_candidate_capture.py:1496–1498`**

```
{"a\ud800": 1, "a\ud801": 2}  ->  {'a�': 2}     # 2 keys -> 1, last wins
```

Benign in practice: `working_set` keys are sha256 hex (`:971`) and `file_progress` keys are
`_SESSION_FILE_RE`-constrained (`:499–501`, fully anchored hex+dashes+`.jsonl`), so neither can carry
a surrogate except by hand-edit. The outcome (one `working_set` entry lost, re-derived on the next
scan) is far milder than the freeze it replaced. Worth a comment, not a fix.

## P3-2 — `append_candidates` does not sanitize at its actual serialization boundary

**`write_candidate_capture.py:1743` vs `:1790`**

The docstring at 1487–1488 says the walk is applied "at each function's actual serialization
boundary." For `save_checkpoint` that is literally true (1952 immediately precedes 1953). For
`append_candidates` it is not: the walk runs at **1743** on the read-back entries only, while the
serialization is at **1790** over `existing` — which by then also holds the new records appended at
**1779**, built after the walk.

Safe today, but **by construction only**, and that construction is not local to this function:
`content`/`supersedes_hint` are laundered by `_truncate_utf8` (1511), `evidence.session_file` is
regex-constrained (1699 ← 506 ← 499–501), `project_ref` is sha256 hex (482), `candidate_id` is a
uuid4, and `trigger`/`kind`/`status`/`polarity` are enum-validated (1682–1707). Any future field
sourced from unconstrained input silently reopens the exact bug round 6 closed. Wrapping the 1790
comprehension costs nothing and makes the docstring's claim true. (The 1743 call must **stay** — the
comment at 1738–1742 is right that the dedup hash at 1746 needs sanitized input.)

---

## Core invariants — re-checked, no regressions

| Invariant | Evidence |
|---|---|
| **`MEMORY.md` never written** | Only two filesystem write sites in the whole module: `atomic_write` at **1791** (`pending.jsonl`) and **1954** (`checkpoint.json`), both under `_project_write_candidates_dir(write_candidates_root, ...)`. No `write_text`/`write_bytes`/`open(...,'w')` anywhere else. `hook.read_memory_documents` (2062) is read-only. |
| **`write_trigger.enabled` defaults false** | `enabled=False` at **195**; strict `isinstance(..., bool)` rejection at **204–205**; `scan()` returns `None` immediately at **2012–2013** when disabled. |
| **Fail-closed broadly** | `_parse_json_fail_closed` `except Exception` (1556); both `scan()` blocks catch-all (2027, 2079); `_main_scan` backstop (2151); `_main_list_pending` now `(WriteCaptureError, UnicodeEncodeError)` (2191). `BaseException` (KeyboardInterrupt/SystemExit) still propagates correctly. |
| **No auto-promotion** | The `promoted` flag (1379/1390–1394) is an internal working-set marker meaning "this repeated observation already produced a candidate" — it gates candidate creation and eviction (`_evict_oldest_unpromoted`, 1424), never a `MEMORY.md` write. Nothing writes outside the candidates root. |
| **Redaction** | `hook.redact` on `content` (1688) and on `supersedes_hint` (1718, round 3's P1-4 residual fix), both then length-bounded. |
| **File permissions** | Dirs `0o700` (340), files `0o600` (1791, 1954), lock `O_CREAT|O_RDWR|O_NOFOLLOW, 0o600` (425); `checkpoint.json` refused if `st_mode & 0o077` (1803–1810) or not owned by the caller (1801). |

---

## Test suite and repo state

```
$ /usr/bin/python3 -m unittest discover -s tests -v
Ran 207 tests in 1.530s
OK
```

207 as claimed (203 + 4). Re-ran after restoring the mutation: still 207 `OK`.

`git status --short` in `完善orca/` — exactly as specified:

- `M claude-codex-memory-bridge/claude_memory_hook.py` — `git diff --stat`: **9 insertions, 1 deletion**,
  the same additive `write_trigger` optional-key change as rounds 2–5, unchanged. Reviewed: it
  tightens to `expected_keys <= set(policy) <= expected_keys | {"write_trigger"}`, so every mandatory
  key is still mandatory and only `write_trigger` is newly tolerated.
- `?? claude-codex-memory-bridge/AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md`
- `?? claude-codex-memory-bridge/tests/test_write_candidate_capture.py`
- `?? claude-codex-memory-bridge/write_candidate_capture.py`

Nothing else under `claude-codex-memory-bridge/`. Other untracked entries at the `完善orca/` top level
are prior rounds' review documents and unrelated concurrent-session directories.

---

## Recommendation

Hand to the human **with P2-1 flagged as a fresh regression**. One small round 7 — replace the
recursive walk with an iterative one, and make `_read_existing_pending` skip-and-count unparseable
lines instead of raising — closes P2-1 and P2-2 together and eliminates the whole
"corrupt-state-file freezes the scanner forever" class rather than the next instance of it.
Add a depth-600 **dict-wrapped** `pending.jsonl` fixture: the existing depth-3000 top-level-list
fixtures cannot reach the code they are meant to protect.
