# Independent read-only adversarial review — install_bridge.py import-error cleanup

- Date: 2026-08-22
- Reviewer: Claude (worker, task_2da84b515bdb / ctx_ac24afd00140 — dispatch capability was
  found revoked and dispatch status `[failed]` when attempting to report; this file is the
  durable record since the orchestration channel would not accept `worker_done`)
- Scope: the import-error-handling cleanup around `_import_write_candidate_capture()` /
  `_wrap_write_candidate_capture_import_error()` and its 3 guarded call sites in
  `claude-codex-memory-bridge/install_bridge.py` (uncommitted worktree changes), plus the
  8 tests in `WriteCandidateCaptureImportErrorGuardTests`
  (`tests/test_install_bridge.py:3911-4140`).
- Interpreter used throughout: `/usr/bin/python3` (3.9.6), per instructions. No edits made,
  read-only review, no coordination with any Opus review.

## What was reviewed

Three call sites all funnel through the shared helpers:

- `_import_write_candidate_capture()` — `install_bridge.py:481`
- `_wrap_write_candidate_capture_import_error(exc, context)` — `install_bridge.py:514`
- Call site A: `verify()` — `install_bridge.py:2982-2984`
- Call site B: `_validate_write_trigger_argument()` — `install_bridge.py:1804-1806`
- Call site C: `_load_write_trigger_config()` — `install_bridge.py:3226-3228` (reached by
  `install_write_trigger()`, both `dry_run=True` and `dry_run=False`, since the load happens
  before either branch)

## Test suite

- Scoped: `python3 -m unittest tests.test_install_bridge.WriteCandidateCaptureImportErrorGuardTests -v`
  → **8/8 pass**.
- Full suite: `python3 -m unittest discover -s tests -p "test_*.py"` → **336/336 pass**, exit 0.
- `python3 -m py_compile install_bridge.py write_candidate_capture.py claude_memory_hook.py` → clean.
- `git status`/`git diff` on the added code: no stray `print`/`TODO`/`FIXME`/`pdb`/`breakpoint` left in.
- Confirmed via `grep` that exactly 3 call sites use `_import_write_candidate_capture()`
  (no unguarded 4th site); `_untracked_owned_including_write_trigger()` deliberately avoids the
  import entirely (uses the hardcoded `WRITE_TRIGGER_BRIDGE_ID` constant instead), by design.

## Independent adversarial matrix (my own harness, not just re-running the given tests)

Built two standalone scripts (not part of the test suite) that construct fresh, `-I`-isolated
fixture directories and drive the real functions/CLI directly:

| Scenario | Call site A (`verify()`, real live handler) | Call site B (`_validate_write_trigger_argument`) | Call site C (`_load_write_trigger_config` / `install_write_trigger` dry+real) | `main()` CLI (dry-run & real) |
|---|---|---|---|---|
| module missing | clean `InstallError` | clean | clean | clean, rc=1, `{"ok": false, ...}` |
| transitive missing (`claude_memory_hook.py` absent) | clean, correctly blames `claude_memory_hook` | clean, correctly blames it | clean, correctly blames it | clean, correctly blames it |
| `write_candidate_capture.py` itself has `SyntaxError` | clean | clean | clean | clean |
| **`claude_memory_hook.py` (transitive dep) has `SyntaxError`** (my own added 4th scenario) | clean, but **misattributed** (see finding) | clean, but misattributed | clean, but misattributed | clean, but misattributed |

Every cell returns rc=0/1 as expected with **no raw traceback, no uncaught
ModuleNotFoundError/SyntaxError/ImportError** anywhere — the fail-closed contract holds in
every combination I tried, including through the real `main()` CLI entrypoint end-to-end for
`install-write-trigger --dry-run` and the real (non-dry-run) path, both under "module missing".

`verify()`'s "module missing" case additionally matches the project's own pre-existing
end-to-end test (`BaseOnlyPathsWithoutWriteCandidateCaptureModuleTests.test_verify_of_a_live_write_trigger_handler_raises_installerror_not_modulenotfounderror`),
which I independently reproduced with a genuinely-installed live handler + a separate
`-I`-isolated child process.

## Finding: P2 — message misattributes a transitive SyntaxError to the wrong file

`_wrap_write_candidate_capture_import_error()` correctly distinguishes, for
`ModuleNotFoundError`/`ImportError`, whether the *module itself* is missing
(`exc.name == "write_candidate_capture"` or `None`) vs. whether *its own transitive
dependency* (`claude_memory_hook`) is missing, and produces an accurate message either way
("...its own dependency 'claude_memory_hook' is unavailable").

For `SyntaxError` it does **not** perform the equivalent check. The code is:

```python
if isinstance(exc, SyntaxError):
    return InstallError(f"{context}: write_candidate_capture module exists but failed to import: {exc}")
```

This branch fires unconditionally whenever *any* `SyntaxError` propagates out of the import —
including one whose `exc.filename` shows it actually came from `claude_memory_hook.py`, not
`write_candidate_capture.py`. Reproduced directly (real repo `write_candidate_capture.py` +
a corrupted `claude_memory_hook.py`):

```
cannot verify write-trigger liveness: write_candidate_capture module exists but failed to
import: invalid syntax (claude_memory_hook.py, line 1)
```

The leading clause claims `write_candidate_capture` "exists but failed to import" (implying
*its own* content is broken), while the actual broken file's name only appears inside the
`(...)` suffix Python's own `SyntaxError.__str__` appends — the same shape of misleading
wording "Issue 1" of this same fix round explicitly called out and fixed for the
`ModuleNotFoundError`/`ImportError` case. An operator debugging a corrupted deploy (e.g. an
interrupted `git checkout`/bad merge that only clobbered `claude_memory_hook.py`) would be
pointed at the wrong file first.

This is **not** a fail-open, crash, or security issue — every call site still raises a clean,
distinguishable `InstallError` in this scenario (confirmed for all 3 call sites + the real CLI,
see table above), so severity is capped at P2 (diagnostic-accuracy only). Flagging because it's
the exact same bug class as an already-fixed "Issue 1", just left open for the `SyntaxError`
branch, and because none of the 8 new tests (nor any other test in the 336-test suite) covers
"transitive dependency has a SyntaxError" — only "transitive dependency missing" and
"write_candidate_capture itself has a SyntaxError" are tested, never the combination of the two.

Suggested fix (not applied — read-only review): in the `SyntaxError` branch, compare
`exc.filename` against the resolved path of `write_candidate_capture.py` itself (available via
the same `_module_dir` logic `_import_write_candidate_capture()` already computes) and emit the
same "...because its own dependency's file failed to parse" wording when they differ.

## Verdict

No P0 or P1 found. One P2 (message misattribution for a transitive-SyntaxError, cosmetic/
diagnostic only, does not affect fail-closed behavior). All 3 guarded call sites verified
independently — not just by re-running the existing suite — to fail closed cleanly across
module-missing, transitive-missing, syntax-corrupted-self, and (my own addition)
syntax-corrupted-transitive, both via direct function calls and via the real `main()` CLI
(dry-run and real). This is a clean-GO-with-one-P2 result, not a blocking finding.

## Note on this dispatch

`orca orchestration send ... --type worker_done ...` for `ctx_ac24afd00140` was attempted and
rejected ("Dispatch ctx_ac24afd00140 capability is revoked"); `dispatch-show` reports the
dispatch as `[failed]` at stage `dispatch_input`, and the inbox/check channel shows no messages
— this appears to be a stale/already-superseded dispatch on the coordinator side, not something
this review caused. This file is the durable record of the completed review in lieu of a
`worker_done` receipt.
