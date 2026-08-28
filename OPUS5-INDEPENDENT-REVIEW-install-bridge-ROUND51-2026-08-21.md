# OPUS5 Independent Review — install_bridge.py §19.3 cleanup round (Round 51)

- **Date**: 2026-08-21
- **Reviewer**: Claude opus5 / max effort, independent, read-only
- **Candidate**: `完善orca/claude-codex-memory-bridge/install_bridge.py` + `tests/test_install_bridge.py` (uncommitted working tree)
- **Scope**: the 5 previously-deferred P2 limitations from design doc §19.3, claimed closed this round
- **Interpreter**: `/usr/bin/python3` → Python 3.9.6 (`/Applications/Xcode.app/Contents/Developer/usr/bin/python3`)
- **Parallel leg**: an independent Codex review ran on the same candidate; no coordination.

---

## Verdict

**GO** — all 5 claimed fixes are genuine, correct, and non-vacuously tested. **0 P0, 0 P1.**
4 P2 and 4 P3 findings are recorded below as the next deferred set, mirroring how §19.3 itself was created.

> **A GO here does NOT authorize running `install-write-trigger`.** Actually installing or activating the
> write-trigger remains the user's own separate, explicit decision, unchanged by this review.

---

## Verification method

Everything below was reproduced against the real module, not read off the summary.

- Real-JSON fixtures built with actual `json.dumps()` / `canonical_json()`, never hand-typed byte strings.
- Both raw-bytes scanners exercised directly, plus the streaming scanner against real on-disk temp files.
- Each of the 5 fixes individually scratch-reverted in an isolated copy; suite re-run to confirm the
  guarding tests fail for the predicted reason.
- Mutation testing of the overlap window and the producer/marker coupling.
- Full suite: `Ran 305 tests ... OK`, exit 0.

---

## Item-by-item confirmation

### Item 1 — double-quote marker was dead code against real JSON — **CONFIRMED FIXED**

`_json_string_body()` (`install_bridge.py:671`) and the escape-pairing loop in
`_bridge_id_marker_value_forms()` (`:728-736`) are real and correct.

Against a genuine `json.dumps()`-serialized hooks.json containing
`--bridge-id "orca-claude-native-memory-v1"`, the on-disk bytes carry `\"…\"` and the scanner now
matches. The value-forms tuple for the default id is:

```
('orca-claude-native-memory-v1', '"orca-claude-native-memory-v1"',
 '\\"orca-claude-native-memory-v1\\"', "'orca-claude-native-memory-v1'")
```

Dedup works as documented (the single-quoted form needs no JSON escaping, so it appears once).

Scratch-revert (`for text in (shape,)`) fails exactly 2 tests, both on-point.

### Item 2 — whitespace variants missed by raw-bytes scanners — **CONFIRMED FIXED**

`_bridge_id_marker_patterns()` (`:739`) is genuinely shared by both scanners (`:800`, `:975`).
Verified Stage 1 / Stage 2 agreement for single space, double space, 8 spaces, tab, CR, LF, and mixed
`space-tab-space`, in real JSON bytes, across all 5 byte encodings (`utf-8`, `utf-16-le/be`,
`utf-32-le/be`) and additionally for BOM-bearing `utf-16`/`utf-32` files.

The mid-fix discovery is real: tab/CR/LF do require JSON escaping on disk, and the same
`_json_string_body()` helper covers it.

`max_len` is a true upper bound (max 236 B = 44 prefix + 8×8 whitespace + 128 suffix for utf-32), and
the derived `overlap_len = 235` is sufficient — a sweep over **every** straddle offset `0..57` for an
8-tab-separated marker found zero misses.

Scratch-revert to `(" ",)` fails exactly 2 tests.

### Item 3 — `make_release()` fragment coupled only by coincidence — **CONFIRMED FIXED (code)**

`_bridge_id_command_fragment()` (`:683`) is genuinely the sole renderer, used at both producer sites:
`:1502` (base command) and `:1568` (write-trigger command). No other production site builds the
fragment.

The regression test at `tests/test_install_bridge.py:963-992` genuinely calls the **real**
`make_release()` — `mock.patch.object` targets only the module constant `BRIDGE_ID`, not any helper —
and traces through the real construction path.

Extended beyond the claimed single-quote case. Drove the real `make_release()` with 8 exotic ids:
`orca-can't-quote-me`, `orca\back\slash`, `orca id with spaces`, `orca"dq`, `orca'"both`,
`orca$(id)`, `orca-é-unicode`, and a 200-char id. **All 8** round-trip through `shlex.split()` and are
found by both Stage 1 and Stage 2, in both `json.dumps()` and `canonical_json()` renderings. Same for
the write-trigger fragment path. The coupling genuinely holds. (See P3-2 for a test-robustness note.)

### Item 4 — unvalidated `bridge_id`/`event` at two entry points — **CONFIRMED FIXED**

`_validate_bridge_id(bridge_id)` and `_validate_hook_event(event)` are the **first two statements** of
`_find_untracked_owned_configs()` (`:1016-1017`) — everything above them is comment, and no filesystem
access precedes them. `owned_handler()` validates at `:464`, before any parsing.

No currently-valid default path regresses: `_validate_hook_event` accepts every real Codex event name
tested (`UserPromptSubmit`, `SessionStart`, `SessionEnd`, `PreToolUse`, `PostToolUse`, `Notification`,
`Stop`, `SubagentStop`, `PreCompact`), and `install()`/`plan()`/`verify()`/`uninstall()`/
`recover_pending_install()` all pass valid defaults.

Both scratch-reverts fail exactly one predicted test each, with the untouched `update_hook_config`
test still passing.

### Item 5 — header comment overstated `make_handler()` — **CONFIRMED FIXED**

`make_handler(command, *, timeout, status_message)` (`:1276-1278`) genuinely has no `event` parameter,
and the corrected header at `:41-44` now says exactly that.

### Item 6 — core invariants — **CONFIRMED INTACT**

- `install_bridge.py` contains **zero** references to `MEMORY.md`.
- `claude_memory_hook.py` has **zero** write sites; both `os.open` calls (`:440`, `:585`) are
  `O_RDONLY | O_NOFOLLOW`. It reads `MEMORY.md` (`:912`) and never writes it.
- `write_candidate_capture.py`'s only 2 write sites (`:492`, `:496`) are the atomic
  `fdopen("wb")` + `os.replace` pair, confined to `RUNTIME_BASE / "write-candidates"`.
  `_record_and_maybe_promote()` (`:1628`) promotes into the checkpoint working set — **not** MEMORY.md.
- Default UserPromptSubmit path is byte-identical:
  `_bridge_id_command_fragment(BRIDGE_ID)` → `--bridge-id orca-claude-native-memory-v1`, **identical**
  to the pre-fix inline `shlex.quote()` rendering. The snapshot test at `:994` pins the exact bytes.

### Item 7 — test suite — **CONFIRMED**

`Ran 305 tests in 2.897s / OK`, exit 0, under Python 3.9.6.

### Item 8 — working tree — **CONFIRMED**

Only 4 tracked files modified: `install_bridge.py`, `claude_memory_hook.py`,
`tests/test_install_bridge.py`, `tests/test_claude_memory_hook.py`.
`prime-agent-integration/` and `round13-review-repros/` ignored as instructed.

### Performance / ReDoS — **no regression**

The new `(?:ws){1,8}` alternation over 20 compiled patterns shows no catastrophic backtracking:
1 MB of spaces 5.0 ms; 1 MB alternating whitespace 4.8 ms; 540 KB of repeated
`--bridge-id` + whitespace-run bait 25.5 ms; a full 4 MiB `MAX_MANAGED_FILE_BYTES` file 19.6 ms.

---

## Findings

### P2-A — `\uXXXX` JSON escaping is not handled: same root cause as the P2-1 this round fixed

**Where**: `install_bridge.py:671` (`_json_string_body`), `:650-657`
(`_BRIDGE_ID_MARKER_WHITESPACE_CHARS` comment), `:717-727` (`_bridge_id_marker_value_forms`).

`_json_string_body()` derives exactly **one** rendering — the one `json.dumps()` happens to produce.
RFC 8259 equally permits `\uXXXX` for any character, and control characters in particular. A conforming
encoder may write `	` for tab, `"` for `"`, `'` for `'`, or `o` for a plain letter.
.NET's `System.Text.Json` does exactly this by default for `"`, `'`, and control characters.

**Reproduced** — 6 of 8 legal-JSON variants diverge (all parse back to the identical command string,
Stage 1 correctly returns True, Stage 2 returns False):

| on-disk form | Stage 1 | Stage 2 |
|---|---|---|
| `--bridge-id\t<id>` (json.dumps default) | True | **True** |
| `--bridge-id	<id>` | True | **False** |
| `--bridge-id <id>` | True | **False** |
| `--bridge-id
<id>` | True | **False** |
| `--bridge-id "<id>"` | True | **False** |
| `--bridge-id o` + `rca-…` | True | **False** |
| `x --bridge-id <id>` | True | **False** |

**Failure scenario**: a relocated, valid, well-formed `hooks.json` larger than
`MAX_MANAGED_FILE_BYTES` (4 MiB) that was last rewritten by such an encoder. The oversized path goes
straight to `_stream_scan_oversized_for_bridge_marker()`, which returns False;
`_find_untracked_owned_configs()` reports nothing; `uninstall()` / `recover_pending_install()` then
delete the receipt while the handler stays live. That is precisely the R11-P1-B / R12-P1-A end-state.

**Root cause is a reasoning gap, not a coding slip**: the comment at `:651-657` argues from "what
`json.dumps()` (and this file's own `canonical_json()`) always renders" — but Stage 2's entire purpose
is scanning files this tool did **not** write. The comment at `:720` has the same shape
("is escaped as `\"` in real hooks.json's on-disk bytes").

**Severity P2, not P1**: needs a non-Python JSON encoder *and* an oversized-or-unparseable file. This
is the same tier §19.3 assigned the double-quote gap that item 1 just closed.

**Suggested**: either add the `\uXXXX` renderings for the marker's own characters to
`_bridge_id_marker_value_forms()` / the whitespace alternation, or explicitly scope the comment to
"encoders that use JSON's short escapes" and record `\uXXXX` as a known Stage-2 limitation.

### P2-B — the widened overlap window is completely untested; the test claiming to cover it does not straddle

**Where**: `tests/test_install_bridge.py:758-770`
(`test_stream_scan_oversized_is_whitespace_tolerant_across_a_chunk_boundary`).

The fixture writes `b"a" * (chunk_size - 5) + raw + b"b" * 2000`, where `raw` is a whole JSON document.
The `--bridge-id` marker sits at **offset 100 inside `raw`**, so it lands **95 bytes after** the 1 MiB
boundary — entirely inside chunk 2. It never straddles anything, contrary to its name and its comment
("including a marker straddling a chunk boundary").

**Proof by mutation**: injecting `overlap_len = 12` at `install_bridge.py:976` leaves
**all 305 tests passing**, while genuinely making the scanner miss straddle offsets **13–54** for an
8-tab-separated marker.

The pre-existing bare-space test (`:655-671`) does straddle correctly, but its marker only needs 5
bytes of overlap. Nothing exercises the 235-byte window this round's fix introduced — the exact
quantity the whitespace fix changed.

**The code is correct** (sweep over every straddle offset `0..57`: zero misses). This is purely a
coverage gap: a future regression in the `max_len` computation would ship silently.

**Suggested**: place the marker itself at `chunk_size - k` for a `k` inside the marker span, e.g.
`b"a" * (chunk_size - raw.find(b"--bridge-id") - 6) + raw`.

### P2-C — `make_release()` is the one bridge-id boundary the round's own validation principle skips

**Where**: `install_bridge.py:1561`, `:1568`.

`write_trigger["bridge_id"]` flows into `_bridge_id_command_fragment()` → `shlex.quote()` with no
`_validate_bridge_id()` anywhere upstream. `make_release()` (`:1423`) is public-named, externally
callable, and reachable through public `install(write_trigger=…)` / `plan(write_trigger=…)` — and it
runs **before** the validating `update_hook_config()` in both (`install()`: `:2018` then `:2129`;
`plan()`: `:2572` then `:2579`).

**Measured behavior** (direct library call):

| value | result |
|---|---|
| `None` | **silently accepted** — `shlex.quote(None)` hits Python's `if not s: return "''"` branch, so the command becomes `--bridge-id ''` and an empty bridge id is baked into `release["write_trigger_command"]` and `release["write_trigger_bridge_id"]` |
| `""` | silently accepted, same `--bridge-id ''` |
| `123` | bare `TypeError`, escaping `main()`'s `except InstallError` at `:2758` |

The `None` case is the notable one: it produces a *registered command carrying an empty bridge id*,
which `owned_handler(bridge_id="")` would now reject via the very validator this round added — making
that handler permanently undetectable and unremovable by this tool.

**Real CLI path is safe**: `_load_write_trigger_config()` (`:2650`) hardcodes `_wtc.MODULE_ID`, a
module constant. So this is library-reachable only.

This is exactly the argument the round makes at `:461-464` for `owned_handler()` ("reachable
independently … so its own bridge_id must be validated here, not only assumed already-valid by an
upstream caller"), applied to 3 of 4 sites. `_bridge_id_command_fragment()` itself is the natural place
for the call.

### P2-D — the untracked-owned safety scan is never run with the write-trigger identity, contradicting its own new comment

**Where**: `install_bridge.py:1909` (`recover_pending_install`), `:2478` (`uninstall`) — both call
`_find_untracked_owned_configs(receipt_paths)` with no `bridge_id=` and no `event=`, so both scans run
under `BRIDGE_ID` / `"UserPromptSubmit"` only.

The comment **this round rewrote**, at `:1027-1028`, states: *"a future caller wiring in
write_candidate_capture.py's SessionEnd handler passes bridge_id=write_candidate_capture.MODULE_ID,
event=\"SessionEnd\" here the same way it would to update_hook_config()."* That caller now **exists**
(`install_write_trigger()`, `:2656`) and does not do it. At both call sites the receipt is in scope and,
for a write-trigger install, carries `write_trigger_bridge_id` (`:2262`, shape-guaranteed by
`:1660-1671`).

**Not currently reachable as a P1** — and I checked this specifically rather than assuming:
`install()` layers both handlers into the **same** `updated[path]` write (`:2119-2136`), so every file
carrying the SessionEnd handler also carries the UserPromptSubmit one, and the default-identity scan
still refuses on relocation. There is also no in-tool path that removes one handler and leaves the
other — `remove=True` appears only as a parameter (`:1314`, `:1366`), never at a call site.

**Still a real gap**: a manual or third-party edit that drops the UserPromptSubmit handler from a
relocated `hooks.json` while leaving SessionEnd makes the safety scan blind, and the comment currently
asserts a behavior the code does not have.

### P3-1 — whitespace runs longer than 8 diverge (documented bound)

`--bridge-id` + 9 spaces + id: Stage 1 True, Stage 2 False. This is the deliberate
`_BRIDGE_ID_MARKER_MAX_WHITESPACE_RUN = 8` bound, explicitly reasoned at `:658-663` (the streaming
scanner needs a finite max match length). Acceptable as documented; noted only because it narrows
rather than eliminates the P2-2 class.

### P3-2 — the producer/marker coupling is enforced only incidentally

No test asserts that `make_release()["command"]` contains `_bridge_id_command_fragment(BRIDGE_ID)`, and
no test calls `_bridge_id_command_fragment()` directly. Replacing the call at `:1502` with a
textually-identical inline expression passes all 305 tests.

A genuinely *different* rendering **is** caught — but by a single, unrelated end-to-end test
(`test_uninstall_catches_a_relocated_account_saved_with_a_utf8_bom`); the write-trigger side is caught
by one test at `:1568`. A one-line direct assertion would make the claimed invariant explicit and
durable.

### P3-3 — stale line references in comments (all verified)

| Comment | Claims | Actual |
|---|---|---|
| `install_bridge.py:64` | `write_candidate_capture.py:60`, `:2237` | `MODULE_ID` is at `wcc:61`; the check is at `wcc:2769` |
| `install_bridge.py:1531-1532` | quotes *"NOT WIRED IN. Example only"* at `wcc:2626-2654` | that phrase appears **0 times** in `wcc`; actual text is *"Illustrative only, kept in sync by hand"* at `wcc:2847` |
| `install_bridge.py:1550` | `RUNTIME_BASE`, `install_bridge.py:92` | `:98` |
| `install_bridge.py:1554` | `_main_scan`, `wcc:2535` | `wcc:2724` |

Comments-only, no behavior. Flagged because this file's comments *are* its design record across 51
rounds, and item 5 of this very round was itself a comment-accuracy fix.

### P3-4 — `"SessionEnd"` triplicated as a bare literal

`:2132`, `:2411`, `:2582` — no module constant, while the authoritative `HOOK_EVENT = "SessionEnd"`
lives at `write_candidate_capture.py:65` and `install_bridge.py` already imports that module at `:2639`.
Compare `DEFAULT_HOOK_EVENT` at `:52`, which does have a constant.

---

## Bottom line

This round did what it said. All 5 §19.3 items are genuinely closed, the fixes are narrow and correct,
the new tests are non-vacuous (each scratch-revert fails the predicted test), the core invariants hold,
and the live UserPromptSubmit path is byte-identical.

The four P2s are the honest residue of a narrow cleanup: one is the same escaping class one level
deeper (`\uXXXX`), one is a coverage gap behind correct code, one is the round's own validation
principle applied to 3 of 4 sites, and one is a stale claim in a comment this round touched. None
blocks.

**GO.** And to restate: this GO does **not** authorize running `install-write-trigger`. That remains a
separate, explicit decision for the user.
