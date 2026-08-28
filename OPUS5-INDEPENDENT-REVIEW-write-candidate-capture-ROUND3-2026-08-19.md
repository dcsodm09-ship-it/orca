# Independent adversarial review — `write_candidate_capture.py`, ROUND 3

**Reviewer:** Claude opus5 / max, independent read-only session (no memory of rounds 1–2; a parallel Codex review ran on the same candidate with no coordination).
**Date:** 2026-08-19
**Candidate:** `完善orca/claude-codex-memory-bridge/write_candidate_capture.py` (1,898 lines), plus `tests/test_write_candidate_capture.py` and `AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md`.
**Method:** full fresh read of the module; every claimed fix re-broken by constructing my own reproductions rather than reading the new regression tests and trusting them.

**Test suite:** `/usr/bin/python3 -m unittest discover -s tests -v` → **186 tests, OK, exit 0.** Matches the claim.

---

## Verdict: **NO-GO**

One P1 (with four independent confirmed reproductions), one P2, five P3s. The P1 is the *same failure shape and same exception type* round 2 flagged and round 3 claimed to close — the fix was applied at one of the module's four JSON-parse sites.

---

## P1-1 — the fail-closed boundary is a type whitelist, not a catch-all; 3 of 4 JSON-parse sites still leak `RecursionError`

The module's hardest stated contracts:

- `write_candidate_capture.py:1681-1684` — *"Fails closed at every step -- any exception here must never propagate ... `main()`'s scan subcommand catches everything and always exits 0."*
- `write_candidate_capture.py:1835` — `return 0  # Always 0: a SessionEnd hook is fire-and-forget`

Round 3 fixed exactly one site. `AUTO-LEARN-TRIGGER-DESIGN-2026-08-19.md:229` says so in its own words: *"Fixed by widening **that one** `try/except` to `except Exception`."* The module has four JSON-parse sites:

| line | site | catch | status |
|---|---|---|---|
| 632 | `_parse_transcript_line` | `except Exception` | **fixed round 3** |
| 1424-1425 | `_read_existing_pending` | `except (UnicodeDecodeError, json.JSONDecodeError)` | **still leaks** |
| 1565 | `load_checkpoint` → `hook.strict_json_loads` | `except hook.BridgeError` | **still leaks** |
| 1791 | `_parse_session_end_input` → `hook.strict_json_loads` | `except hook.BridgeError` | **still leaks** |

`claude_memory_hook.py:113-117` `strict_json_loads` catches only `(UnicodeDecodeError, json.JSONDecodeError, BridgeError)`. `RecursionError` subclasses `RuntimeError`, so it passes straight through — and past `scan()`'s two clauses (`:1714` `(WriteCaptureError, hook.BridgeError, ImportError)`, `:1765` `(WriteCaptureError, hook.BridgeError)`) and past `_main_scan`'s (`:1833` `(WriteCaptureError, OSError, ValueError)`).

### Reproduction A — the shipping CLI exits 1 with a traceback (worst outcome)

A 32,067-byte stdin payload — half the module's own 65,536-byte bound at `:1788`:

```
$ /usr/bin/python3 write_candidate_capture.py scan \
    --bridge-id orca-claude-codex-memory-write-trigger-v1 \
    --policy /nonexistent/policy.json \
    --expected-policy-sha256 aa --expected-script-sha256 bb < deep_stdin.json
Traceback (most recent call last):
  ...
  File ".../write_candidate_capture.py", line 1825, in _main_scan
    cwd, session_id = _parse_session_end_input(stdin)
  File ".../write_candidate_capture.py", line 1791, in _parse_session_end_input
    payload = hook.strict_json_loads(raw)
  File ".../claude_memory_hook.py", line 115, in strict_json_loads
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
RecursionError: maximum recursion depth exceeded while decoding a JSON array from a unicode string
REAL CLI EXIT=1
```

Payload: `{"hook_event_name":"SessionEnd","cwd":"/tmp","session_id":"a","x":` + `"["*16000` + `"]"*16000 + `}`. This is a *well-formed* SessionEnd envelope — it passes every shape check the module makes — that is simply deeply nested. Exit 1 + traceback on stderr from a `SessionEnd` hook is precisely what the "always 0, fire-and-forget" contract exists to prevent.

### Reproductions B and C — `scan()` raises, end-to-end, on the module's own on-disk state

Using the project's own `WriteCaptureFixture`, an otherwise-valid scan that would write one T1 candidate:

```
--- deeply-nested checkpoint.json ---   checkpoint: !! scan() RAISED RecursionError
--- deeply-nested pending.jsonl ---     pending:    !! scan() RAISED RecursionError
--- control: clean run ---              clean:      scan() returned ScanResult (contained)
```

Both files live in a user-writable directory and are explicitly treated as untrusted elsewhere in the same functions (`load_checkpoint` validates mode, size, schema, `project_ref`, and — since round 3 — per-entry `working_set` shape; `_read_existing_pending` raises `WriteCaptureError` on a corrupt entry). Round 2's accepted P1 was an uncaught `AttributeError` from a malformed `checkpoint.json` `working_set` entry — same file, same trust model, same "malformed on-disk state must fail closed" requirement. `_read_existing_pending` also escapes `list_pending` → `_main_list_pending` (`:1846` catches only `WriteCaptureError`) → traceback, exit 1.

**Why the round-3 regression test did not catch this:** `TranscriptParsingFailClosedTests` (tests:1210-1265) uses the correct triggering input (`"["*3000 + "]"*3000`) and even sanity-checks that it genuinely raises `RecursionError` — but only ever plants it in a **transcript file**, the one path that was fixed. This is round 1's own warning recurring for the third round: *the fix's regression test tests the site that was fixed, not the failure shape.*

### Reproduction D — same root cause, different code path: lone surrogate → permanent per-project capture stall

Not a JSON-parse site. `_truncate_utf8:1385`, the dedup hashes at `:1490/:1510/:1526`, and the payload build at `:1534` all call `.encode("utf-8")` on transcript-derived text. A lone surrogate raises `UnicodeEncodeError` out of `scan()`:

```
attempt 1: !! scan() RAISED UnicodeEncodeError: 'utf-8' codec can't encode character '\ud800' in position 45
attempt 2: !! scan() RAISED UnicodeEncodeError: ...
attempt 3: !! scan() RAISED UnicodeEncodeError: ...
checkpoint written? False
```

Broader fuzz (10 adversarial turn bodies) isolated it to exactly the surrogate family; everything else is contained:

```
!! ESCAPE lone surrogate (matched turn)  -> UnicodeEncodeError
!! ESCAPE lone surrogate low             -> UnicodeEncodeError
!! ESCAPE surrogate pair split           -> UnicodeEncodeError
OK NUL byte / 10MB single turn / deep combining / 5000 URLs / RTL-bidi / 300KB base64 / control chars
```

Lone surrogates are *legitimately* present in real Claude Code transcripts: Node's well-formed `JSON.stringify` (ES2019) emits them as `\udXXX` escapes rather than failing, and `json.loads` faithfully reconstructs them.

Consequence is worse than the raise itself: `checkpoint.file_progress` is assigned and saved only at `:1756-1757`, *after* `append_candidates`. Any raise in between means **no checkpoint advance** — every future `SessionEnd` re-reads the same poisoned record and dies again. `byte_offset` is frozen permanently (until 30-day transcript rotation). That is exactly the harm shape of round-2's P1-3.

`UnicodeEncodeError` subclasses `ValueError`, so `_main_scan` *does* catch this one and the CLI still exits 0 — on reachability alone I would rate D a P2. I am reporting it under P1 because it shares one root cause and one fix with A–C, not to inflate the count.

### Root cause and fix

`scan()` and `_main_scan()` enumerate expected exception types. The contract requires the inverse. Catch `Exception` at both boundaries (`KeyboardInterrupt`/`SystemExit` derive from `BaseException` and will still propagate correctly), and widen the three remaining JSON-parse catches to `except Exception` the way `_parse_transcript_line:633` already does. Separately, consider advancing `file_progress` in a `finally`/incremental fashion so a poisoned record cannot freeze the cursor forever.

---

## P2-1 — `MAX_RECORD_BYTES` is not enforced *during* the record-boundary search, only retroactively

`_advance_record_search:678-747` never compares `seeking_bytes` against `MAX_RECORD_BYTES`. The ceiling is consulted only at `:733`, after a terminating newline has been found. The search itself is unbounded.

Measured with constants scaled down 1000× (`MAX_SCAN_BYTES_PER_INVOCATION=4096`, `MAX_RECORD_BYTES=65536`) against one 1,000,012-byte record:

```
scan   1: byte_offset=0 seeking=  4096   scan 100: byte_offset=0 seeking= 409600
scan   2: byte_offset=0 seeking=  8192   scan 200: byte_offset=0 seeking= 819200
scan  50: byte_offset=0 seeking=204800   RESOLVED at scan 245: skipped=1
```

The code knew at scan 17 (`seeking > MAX_RECORD_BYTES`) that this record must exceed the ceiling and would be discarded; it kept searching for another **228 scans**. At production constants a 1 GiB unterminated record costs ~256 `SessionEnd` invocations instead of ~17.

This contradicts the constant's own docstring at `:96-102`: *"hard ceiling on how large a single JSONL record is ever trusted to be **before it is given up on as unparseable rather than searched for indefinitely**."* It is searched indefinitely; only the *parse* is bounded.

Mitigating (why P2, not P1): forward progress is strictly monotonic, the outcome is terminal and correctly counted, memory stays bounded, and the ascending-unread-length sort at `:820` keeps the pathological file last so sibling files are never starved. Fix is one comparison: once `seeking_bytes >= MAX_RECORD_BYTES`, stop searching and take the forced-skip branch.

---

## P3 findings

1. **`:1354` `content=entry.get("sample", text)[:800]`** — the surviving instance of the exact truncate-before-redact pattern round 3 removed from `_match_t1_t2`/`_match_t4`/`_match_t6`/`_match_t7`/`_failure_signatures`. The `sample` path is safe (already redact-then-truncated at `:1339`/`:1610`); the `text` fallback truncates raw text before `make_candidate_record` redacts it. Currently **unreachable** — `load_checkpoint:1595` enforces `isinstance(sample, str)` and `:1337` always sets it — so this is hardening, not a live leak. Change the fallback to `hook.redact(text)`.
2. **`:734-737`** — when `_read_exact_span` returns `None` (TOCTOU, permission change), `byte_offset` still advances past the record at `:744` but the record is *not* counted in `unparseable_records_skipped`. A silently-skipped record, invisible to the very accounting this fix added.
3. **`:382` `import install_bridge` inside `atomic_write`** — `ImportError` is caught by `scan()`'s *first* block (`:1714`) but not its second (`:1765`), which `save_checkpoint`/`append_candidates` run inside. Unreachable in production (the default-root import happens in block 1), reachable for any caller passing `write_candidates_root` explicitly — i.e. every test, and any future in-process wiring.
4. **`:612` `os.pread` of up to 64 MiB** in `_read_exact_span` can raise `MemoryError`, which its `except OSError` does not catch. Rolled into the P1-1 fix if the boundary becomes `except Exception`.
5. **`:842`** — `_advance_record_search` resolves at most one record per invocation, then `continue`s, discarding this file's remaining budget. Throughput only.

---

## Repo state — **`prime-agent-integration/` IS modified** (process finding, not a code defect)

`git status --short` in `完善orca/` at the *start* of this review showed one modified tracked file. It now shows two:

```
 M claude-codex-memory-bridge/claude_memory_hook.py
 M prime-agent-integration/install_prime_agent.py     <-- 73 insertions(+), 10 deletions(-)
```

`prime-agent-integration/install_prime_agent.py` was **not** modified at 22:5x when I started; it appeared at 23:02:16 and its mtime kept advancing while I reviewed (23:02:16 → 23:02:34). Its diff is a "Round 32 ... independent Claude opus/max round-31 review, P1" change to the prime-agent launch guard / command wrapper digest pinning — an unrelated, actively-in-progress workstream from another concurrent session on this machine.

It is **functionally independent** of this candidate (`write_candidate_capture.py` imports only `claude_memory_hook` and `install_bridge`), so it does not affect the code verdict. But the working tree is not clean, so the handoff must scope its commit to explicit paths — a `git commit -a` or `git add -A` would sweep in a half-written prime-agent installer.

The candidate's own files were stable throughout (22:41–22:51). The `claude_memory_hook.py` diff is unchanged from round 2 and correct:

```python
optional_keys = {"write_trigger"}
if not (expected_keys <= set(policy) <= expected_keys | optional_keys):
    raise BridgeError("unexpected policy keys")
```

9 insertions, 1 deletion. Every mandatory key still required; exactly one new optional key tolerated.

---

## Genuinely closed — I tried and could not break these

| Item | How I verified | Result |
|---|---|---|
| **P1-3/P1-A** record-boundary progress | 245-scan loop over a 15×-ceiling record, asserting strict monotonicity | Strictly increasing every scan, terminal, `unparseable_records_skipped=1`. **Genuinely fixed.** No off-by-one: `search_from = start_byte + len(raw)` reconciles exactly in both the no-newline (`position=0`) and partial (`position>0`) cases |
| Unterminated-tail open item | 50 KB never-newline-terminated file beside a healthy file, 3 scans | Healthy file's turn read on scan 1; seeking advances 3862→7958→12054; `oversized=1` correctly reported. Blast radius genuinely bounded, honestly documented |
| **P1-1** broad `except Exception` at `:633` | Read scope; checked `BaseException` hierarchy | Correct and *narrowly* scoped (2 operations). Does **not** swallow `KeyboardInterrupt`/`SystemExit`. Swallowing `MemoryError` there is consistent with fail-closed, not a masking risk |
| **P1-1** `working_set` per-entry validation | Malformed entries + downstream `build_candidates` | Dropped on load, no `AttributeError` |
| **P1-4** truncate-before-redact | Real 40-line PEM through T1 | **0/40** body lines leaked; `_LONG_BLOB_RE` additionally covers per-line splits in T3/T5 |
| **P1-4** `supersedes_hint` | Secret token in a MEMORY.md heading via a real T7 match | `supersedes_hint='Deploy token [REDACTED_TOKEN] rotation policy'` |
| **P1-4** working-set migration | Pre-fix unredacted `checkpoint.json`, load + resave | Token gone after load *and* after resave |
| **P1-4** T5 cross-session | `sk-ant-…` in a failure signature, 2 sessions | Absent from both `checkpoint.json` and `pending.jsonl` |
| Exact-dedup over truncated text | Identical over-limit content scanned twice | **1** pending record, not 2 |
| `checkpoint.json` 0600 | Modes 0600/0640/0604/0660 | Only 0600 accepted |
| `MEMORY.md` invariant | Bytes + `mtime_ns` before/after a writing scan; static audit | Byte-identical, mtime unchanged; no promotion function exists anywhere |
| No auto-promotion | Symbol grep | None |
| Policy defaults | Base v1 policy | `enabled=False`; validates unchanged by the live read hook |
| Policy additive block | `write_trigger`-bearing policy | Accepted by live `hook.validate_policy` — round-1 P1-5 stays closed |
| File permissions | `pending.jsonl`, `checkpoint.json` after a real scan | `0o600` both |
| Regex safety / hangs | 10 adversarial inputs incl. 10 MB turn, 300 KB base64, 5000 URLs, 25s alarm | No catastrophic backtracking, no hangs |

Also confirmed as stated (not defects): `_main_scan` discards `scan()`'s return value, so `oversized_files_skipped`/`records_permanently_skipped` have no live consumer; the module is unwired.

---

## Bottom line

The round-3 work is real and most of it is correct — P1-3/P1-A's incremental record search is a genuine, well-engineered fix that I could not stall, and all three P1-4 redaction gaps verify closed against real secret material. But the P1-1 fix closed one of four instances of its own failure shape, and the missed instances are strictly worse than the one that was fixed: the shipping CLI now exits 1 with a traceback on a 32 KB well-formed hook payload.

This is round 3 of the same lesson. **NO-GO** — not ready to hand to a human as a reviewed, deployable-pending-authorization candidate. Re-review after the boundary is made a catch-all rather than a type whitelist.
