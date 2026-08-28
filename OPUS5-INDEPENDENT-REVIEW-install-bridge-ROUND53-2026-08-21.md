# Independent adversarial review — install_bridge.py write_trigger follow-up fix (Round 53)

Reviewer: Claude opus5 / max, read-only, independent (parallel Codex leg ran with no coordination).
Date: 2026-08-21. Interpreter: `/usr/bin/python3` 3.9.6 (verified, not Homebrew 3.14.6).
Candidate: `/Volumes/Extreme SSD/Orca/workspaces/orca/完善orca/claude-codex-memory-bridge/install_bridge.py`

**Verdict: NO-GO** (1 × P1, 3 × P2, 1 × P3).

---

## Verified claims (all genuine)

| Claim | Result |
|---|---|
| `_validate_write_trigger_argument()` reuses the real runtime validator | **Confirmed at runtime.** Patched `wtc._parse_write_trigger_block` and observed the real function called with `{'enabled': True, 'max_candidates_per_project': 50, 'max_candidate_bytes': 1500}`. Not a reimplementation. |
| Validation before ANY file I/O in `make_release()` | **Confirmed empirically.** Instrumented `resolve_ssd_path` / `validate_owned_file` / `discover_hook_configs`; **zero** calls before rejection. Statement order: 1693–1706 is the first executable code; first I/O is 1711. |
| 316 tests pass | **Confirmed.** `Ran 316 tests ... OK` on 3.9.6. |
| Item 1 revert-verified | **Independently reproduced** (monkeypatch, repo untouched): reverting to the prior single-field validator → **18 failures + 2 errors**. |
| Item 2 revert-verified | **Independently reproduced**: reverting to the receipt-gated scan → **exactly the 1 new test fails**; the older P2-D test still passes. Cleanly isolated. |
| `git status --short` scope | Within `claude-codex-memory-bridge/`: exactly 4 modified + 4 untracked expected files. |

---

## P1-1 — Unconditional `write_candidate_capture` import bricks the base-only install/uninstall/recover path

`install_bridge.py:1457` (`_untracked_owned_including_write_trigger`) → `install_bridge.py:478` (`_import_write_candidate_capture`)

Item 2 made the write-trigger identity scan unconditional. It derives `MODULE_ID` by **importing** `write_candidate_capture`, with no guard. That import now sits on the base-only `uninstall()` (2761) and `recover_pending_install()` (2187) paths, where it never was before — the prior version gated on the receipt field, a plain string needing no import.

Verified with a **real-file** repro (copied `install_bridge.py` + `claude_memory_hook.py` into a clean dir without `write_candidate_capture.py`): raises `ModuleNotFoundError` (an `ImportError` subclass), which `main()`'s `except InstallError` (3039) does **not** catch.

Reproduced aftermath on a plain, base-only install:

```
install  -> InstallError "install failed and the durable recovery journal remains pending"
            pending journal left = True; receipt exists = True
            main/acct UserPromptSubmit handlers = 2   (hooks.json ALREADY modified)
recover  -> BARE ImportError
uninstall-> BARE ImportError
install  -> BARE ImportError
verify   -> InstallError "pending install journal must be recovered first"
main()   -> UNCAUGHT ImportError escapes; NO JSON on stdout
```

Total lockout in every direction, with the live `hooks.json` already changed and the tool's own JSON output contract broken. The base bridge is live in the user's real `hooks.json` today, so `uninstall` is the emergency escape hatch — and it is the first thing that breaks.

Not hypothetical: **`write_candidate_capture.py` is currently untracked in git** while `install_bridge.py` is tracked+modified. Committing one without the other produces exactly this. Also triggered by any import-time error in `write_candidate_capture.py` or in the `claude_memory_hook` it imports at module level.

**Suggested fix:** define `WRITE_TRIGGER_BRIDGE_ID = "orca-claude-codex-memory-write-trigger-v1"` as a module constant (the value is already spelled out in comments at 63–75), use it in the safety net, and add a test asserting it equals `wtc.MODULE_ID`. This removes the import from the rollback path entirely while preserving item 2's guarantee. Wrapping the import in `try/except` instead trades a crash for a silently-disarmed safety net — the very thing item 2 fixed.

## P2-1 — Unknown-key rejection silently dropped; bare `TypeError` still reachable

`install_bridge.py:1655-1667`, harm at `1741-1742`

`_parse_write_trigger_block()` enforces `set(raw) != expected → WriteCaptureError("unexpected write_trigger keys")` (`write_candidate_capture.py:274-276`). Rebuilding a synthetic dict from `.get()` means that check never sees the caller's real key set. Then `release_key_fields["write_trigger"] = write_trigger` (1741) hands the **whole caller dict** to `canonical_json()` (1742). Verified:

- extra key holding a `set` / `Path` / `bytes` / `object` → **bare `TypeError: Object of type X is not JSON serializable`** (also through the full `install()` path)
- a non-string extra key → **bare `TypeError: '<' not supported between instances of 'int' and 'str'`** from `sort_keys=True`
- a serializable extra key → silently **accepted**; because `write_trigger` is a release-key field it yields a different `release_id` / `release_dir` / `policy.json` bytes / command for a semantically identical config, with `install()` and `verify()` both reporting ok

Same bare-exception class item 1 exists to eliminate, and the same reachability argument the fix's own comment (1674–1680) makes. One-line fix: assert the exact 3-key set before building the synthetic dict.

## P2-2 — Identity set is `MODULE_ID` + receipt field only; a version bump orphans old handlers

`install_bridge.py:1458-1461`

Verified: `install_write_trigger` under a non-default bridge_id → plain `install()` → the custom identity is lost and the orphan is missed. More realistically, patching `MODULE_ID` `-v1` → `-v2`:

```
K: after MODULE_ID v1->v2 bump, old v1 orphan caught = False
K: uninstall SUCCEEDED, abandoning the live v1 handler
```

The comment at 1448–1451 claims "a receipt that ever recorded a different, non-default identity is still covered, not silently dropped" — **false** exactly when a plain install has reset the field, which is the scenario the fix exists for. Not CLI-reachable today (`_load_write_trigger_config` hardcodes `_wtc.MODULE_ID` at 2931), but the constant is literally named `-v1`. Fix: scan a frozen tuple of known historical write-trigger identities.

## P2-3 — Third gap, same family: `verify()` still trusts the resettable receipt field

`install_bridge.py:2652-2656` and `2691-2695`

`verify()` gates the **entire** write-trigger check on `receipt.get("write_trigger_script_sha256") is not None` — the identical resettable-receipt-field pattern item 2 just fixed for the safety net. After the exact sequence item 2's own new test performs (its test asserts `assertNotIn("write_trigger_script_sha256", receipt2)` at test line 3274):

```
P: after install-write-trigger, verify has wt sha: True
P: receipt lost wt fields: True
P: verify ok: True | reports wt sha: False
P: SessionEnd handler STILL live: 1
```

`verify()` returns `ok: true`, omits `write_trigger_script_sha256`, and stops checking both the SessionEnd handler count and the installed `write_candidate_capture.py` digest — while that handler is still firing on every Codex session. Item 2 hardened one consumer of the field and left its sibling.

Bounded by mitigations I verified: the tracked-config whole-file `after_sha256` check still catches edits; the registered command is hash-pinned so a tampered script fails closed at runtime; release dirs are never pruned by this tool (1211–1221); and `uninstall()` still cleans up correctly via the carried-forward uninstall baseline. Real harm is observability, not corruption — but it is the same inconsistency.

## P3-1 — `sys.path` grows unbounded

`install_bridge.py:478` — `sys.path.insert(0, ...)` on every call, no dedupe. Verified +50 entries after 50 calls. Pre-existing pattern, but item 2 made it fire on every uninstall/recover. Guard with `if p not in sys.path`.

---

## Checked and genuinely fine

- **Exotic bridge_ids** — unicode, space, `#`, newline, tab, single/double quote, backslash, `--policy`, 100 000 chars: all round-trip correctly through both `owned_handler()` and the real file-level `_find_untracked_owned_configs()` scan. The marker-form machinery (`_bridge_id_command_fragment` / `_bridge_id_marker_value_forms` / `\uXXXX` variants) is genuinely robust.
- **Orderings** — plain→write-trigger (reverse) caught; write-trigger→plain caught (the fix); write-trigger→write-trigger→plain caught for the default identity.
- **No false positives / overhead** — a machine that never had write-trigger installed yields an empty scan and `uninstall()` proceeds normally. Cost is one extra `_find_untracked_owned_configs()` pass per uninstall/recover.
- **`uninstall()` after a plain reinstall** — I suspected it would abandon the SessionEnd handler; it does not. The carried-forward uninstall baseline restores fully pristine content. Hypothesis disproved.
- **MEMORY.md never written** — `install_bridge.py` never references it; `claude_memory_hook.py` only reads it (919); `write_candidate_capture.py`'s docstring (14–16) states every write goes to a separate write-candidates root and promotion into MEMORY.md is explicitly out of scope. The `promoted` flag is an internal checkpoint marker.
- **Base UserPromptSubmit path byte-identical** — policy has exactly the 9 base keys with no `write_trigger`; result has no `write_trigger_*` keys; command never mentions `write_candidate_capture`; `make_handler()` default is unchanged (`timeout: 5`, `statusMessage: "Loading Claude memory from verified SSD"`); base install emits only `UserPromptSubmit`, preserves the pre-existing handler at index 0, verify ok, uninstall pristine; `make_release()` with `write_trigger=None` never imports `write_candidate_capture` even with the import blocked. **Caveat:** emitted bytes are identical, but the base *operational* path is no longer import-independent — that is P1-1.

---

## Verdict

**NO-GO**, on P1-1 alone. P2-1/P2-2/P2-3 are each a one-to-few-line hardening in the same functions.

The two claimed fixes are, on their own merits, real, correctly implemented, genuinely reuse the authoritative validator rather than reimplementing it, and are load-bearing under independent revert-verification. P1-1 is a side effect of *how* item 2 obtains the identity, not of the decision to scan unconditionally — which is correct.

**A GO would not itself authorize running `install-write-trigger`.** That remains the user's own separate, explicit decision, and nothing in this review constitutes it.
